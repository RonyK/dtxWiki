from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import subprocess
import sys
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from types import FunctionType
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session

from backend.app.bootstrap import (
    current_alembic_head,
    ensure_app_version_synced,
    migration_status,
)
from backend.app.settings import Settings
from backend.app.version import source_version
from backend.models.api_token import ApiToken
from backend.models.app_metadata import AppMetadata
from backend.models.base import Base
from backend.models.context import (
    ContextCitation,
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.trust_boundaries import actor_can_read_restricted_library
from backend.services.credential_hashing import CredentialHasher
from backend.services.page_service import sync_page_projections
from backend.tests.utils.environment import child_env_without_dtxwiki_config
from backend.tests.utils.graph_bench_manifest import (
    file_sha256,
    git_fingerprint,
    sqlite_content_digest,
    sqlite_schema_digest,
    sqlite_schema_records,
)
from backend.tests.utils.workspace_graph_corpora import (
    BENCHMARK_PASSWORD,
    WorkspaceGraphCorpus,
    benchmark_actor,
    build_synthetic_corpus,
    materialize_synthetic_corpus,
    seed_benchmark_principals,
)
from backend.tests.utils.workspace_graph_fixture_identity import fixture_time
from backend.tests.utils.workspace_graph_synthetic import (
    SYNTHETIC_GENERATOR_VERSION,
    context_title,
    plan_synthetic_corpus,
    synthetic_plan_digest,
)
from scripts.bench.graph import build_corpus as build_corpus_script

CanonicalRows = tuple[tuple[str, ...], ...]
CorpusSnapshot = tuple[tuple[tuple[str, CanonicalRows], ...], ...]
_CORPUS_CLI_SOURCE_GROUP = "graph-bench-live-checkout"
_CORPUS_CLI_PATH = "scripts/bench/graph/build_corpus.py"


def _assert_corpus_cli_source_group(cases: Mapping[str, FunctionType]) -> None:
    for name, case in cases.items():
        groups = [
            mark.args[0] if mark.args else mark.kwargs.get("name")
            for mark in getattr(case, "pytestmark", ())
            if mark.name == "xdist_group"
        ]
        # xdist combines multiple group marks into a different scheduling key.
        # Merely containing the intended group would therefore not serialize it.
        assert groups == [_CORPUS_CLI_SOURCE_GROUP], (
            f"{name} uses the shared checkout and must have exactly one "
            f"xdist_group({_CORPUS_CLI_SOURCE_GROUP!r}); found {groups!r}"
        )


def _canonical_rows(session: Session, statement: Any) -> CanonicalRows:
    return tuple(
        tuple("" if value is None else str(value) for value in row)
        for row in session.execute(statement)
    )


def _build_snapshot(
    *,
    documents: int,
    profile: str,
    seed: int,
    workspaces: int = 1,
    restricted_library_pct: float = 0,
) -> tuple[list[WorkspaceGraphCorpus], CorpusSnapshot]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        corpora = build_synthetic_corpus(
            session,
            documents=documents,
            profile=profile,
            seed=seed,
            workspaces=workspaces,
            restricted_library_pct=restricted_library_pct,
        )
        snapshot = tuple(
            (
                (
                    "context_pages",
                    _canonical_rows(
                        session,
                        select(ContextPage.id, ContextPage.current_version_id)
                        .where(ContextPage.workspace_id == corpus.workspace_id)
                        .order_by(ContextPage.id),
                    ),
                ),
                (
                    "context_versions",
                    _canonical_rows(
                        session,
                        select(
                            ContextPageVersion.id,
                            ContextPageVersion.page_id,
                            ContextPageVersion.content_hash,
                        )
                        .where(
                            ContextPageVersion.workspace_id == corpus.workspace_id
                        )
                        .order_by(ContextPageVersion.id),
                    ),
                ),
                (
                    "library_items",
                    _canonical_rows(
                        session,
                        select(LibraryItem.id, LibraryItem.current_version_id)
                        .where(LibraryItem.workspace_id == corpus.workspace_id)
                        .order_by(LibraryItem.id),
                    ),
                ),
                (
                    "library_versions",
                    _canonical_rows(
                        session,
                        select(
                            LibraryItemVersion.id,
                            LibraryItemVersion.library_item_id,
                        )
                        .where(
                            LibraryItemVersion.workspace_id == corpus.workspace_id
                        )
                        .order_by(LibraryItemVersion.id),
                    ),
                ),
                (
                    "tree_nodes",
                    _canonical_rows(
                        session,
                        select(
                            WorkspaceTreeNode.id,
                            WorkspaceTreeNode.parent_id,
                            WorkspaceTreeNode.node_kind,
                            WorkspaceTreeNode.target_kind,
                            WorkspaceTreeNode.target_id,
                        )
                        .where(
                            WorkspaceTreeNode.workspace_id == corpus.workspace_id
                        )
                        .order_by(WorkspaceTreeNode.id),
                    ),
                ),
                (
                    "page_links",
                    _canonical_rows(
                        session,
                        select(
                            PageLink.id,
                            PageLink.source_page_id,
                            PageLink.source_version_id,
                            PageLink.target_page_id,
                        )
                        .where(PageLink.workspace_id == corpus.workspace_id)
                        .order_by(PageLink.id),
                    ),
                ),
                (
                    "citations",
                    _canonical_rows(
                        session,
                        select(
                            ContextCitation.id,
                            ContextCitation.target_id,
                            ContextCitation.library_item_version_id,
                            ContextCitation.cited_context_page_version_id,
                        )
                        .where(
                            ContextCitation.workspace_id == corpus.workspace_id
                        )
                        .order_by(ContextCitation.id),
                    ),
                ),
                (
                    "projections",
                    _canonical_rows(
                        session,
                        select(
                            EdgeProjection.id,
                            EdgeProjection.source_id,
                            EdgeProjection.target_kind,
                            EdgeProjection.target_id,
                            EdgeProjection.edge_kind,
                        )
                        .where(EdgeProjection.workspace_id == corpus.workspace_id)
                        .order_by(EdgeProjection.id),
                    ),
                )
            )
            for corpus in corpora
        )
    engine.dispose()
    return corpora, snapshot


def _context_degree_median(session: Session, corpus: WorkspaceGraphCorpus) -> float:
    context_ids = set(corpus.context_ids)
    neighbors: dict[UUID, set[UUID]] = {
        context_id: set() for context_id in context_ids
    }
    rows = session.execute(
        select(
            EdgeProjection.source_id,
            EdgeProjection.target_id,
        ).where(
            EdgeProjection.workspace_id == corpus.workspace_id,
            EdgeProjection.target_kind == "context_page",
            EdgeProjection.edge_kind.in_(("wikilink", "citation_page")),
        )
    )
    for source_id, target_id in rows:
        if (
            source_id not in context_ids
            or target_id not in context_ids
            or source_id == target_id
        ):
            continue
        neighbors[source_id].add(target_id)
        neighbors[target_id].add(source_id)
    return statistics.median(len(values) for values in neighbors.values())


def test_synthetic_planner_preserves_historical_topology() -> None:
    expected = {
        1_000: {"wikilink": 2_623, "citation_source": 373, "citation_page": 83},
        2_000: {"wikilink": 5_090, "citation_source": 753, "citation_page": 188},
        5_000: {
            "wikilink": 13_110,
            "citation_source": 1_753,
            "citation_page": 473,
        },
    }

    for documents, relation_counts in expected.items():
        plan = plan_synthetic_corpus(
            documents=documents,
            profile="synthetic",
            seed=20260917 + documents,
            corpus_key=f"scale-{documents}",
        )
        assert plan.relation_counts == relation_counts
        assert plan.corpus_key == f"scale-{documents}"

    assert context_title(0) == "Search note 000000"
    assert context_title(19) == "Graph note 000019"
    assert context_title(20) == "Search note 000020"


def test_sparse_v3_topology_counts_are_explicit() -> None:
    plan = plan_synthetic_corpus(
        documents=1_000,
        profile="sparse",
        seed=41,
    )

    assert plan.relation_counts == {
        "wikilink": 180,
        "citation_source": 90,
    }
    assert sum(
        relation.link_syntax == "wikilink" for relation in plan.relations
    ) == 45
    assert sum(
        relation.link_syntax == "context_url" for relation in plan.relations
    ) == 135
    assert sum(
        relation.edge_kind == "citation_source" for relation in plan.relations
    ) == 90
    assert len(plan.body_links) == 180
    assert sum(link.syntax == "wikilink" for link in plan.body_links) == 45
    assert sum(link.syntax == "url" for link in plan.body_links) == 135


def test_high_density_multiplier_preserves_base_prefix_and_kind_mix() -> None:
    base = plan_synthetic_corpus(
        documents=1_000,
        profile="synthetic",
        seed=20261917,
        corpus_key="scale-1000",
    )
    dense = plan_synthetic_corpus(
        documents=1_000,
        profile="synthetic",
        seed=20261917,
        relation_multiplier=4,
        corpus_key="scale-1000-density-4x",
    )

    assert dense.relations[: len(base.relations)] == base.relations
    assert base.body_links == dense.body_links == ()
    assert dense.relation_counts == {
        edge_kind: count * 4
        for edge_kind, count in base.relation_counts.items()
    }
    assert synthetic_plan_digest(dense) != synthetic_plan_digest(base)
    assert dense == plan_synthetic_corpus(
        documents=1_000,
        profile="synthetic",
        seed=20261917,
        relation_multiplier=4,
        corpus_key="scale-1000-density-4x",
    )

    with pytest.raises(ValueError, match="supported only for synthetic"):
        plan_synthetic_corpus(
            documents=1_000,
            profile="sparse",
            seed=41,
            relation_multiplier=4,
        )
    with pytest.raises(ValueError, match="unique-relation capacity"):
        plan_synthetic_corpus(
            documents=1_000,
            profile="synthetic",
            seed=20261917,
            relation_multiplier=242,
        )

    exact_capacity = plan_synthetic_corpus(
        documents=10,
        profile="synthetic",
        seed=25,
        relation_multiplier=9,
    )
    assert exact_capacity.relation_counts == {
        "wikilink": 72,
        "citation_source": 9,
        "citation_page": 18,
    }
    assert len(
        {
            (
                relation.edge_kind,
                relation.source_index,
                relation.target_kind,
                relation.target_index,
            )
            for relation in exact_capacity.relations
        }
    ) == len(exact_capacity.relations)


def test_synthetic_plans_have_pinned_digests_and_seed_independence() -> None:
    synthetic = plan_synthetic_corpus(
        documents=2_000,
        profile="synthetic",
        seed=260917,
    )
    sparse = plan_synthetic_corpus(
        documents=1_000,
        profile="sparse",
        seed=41,
    )

    assert synthetic_plan_digest(synthetic) == (
        "4b215ad6aa90d83d8417009ac6aa3cbdbe9ece9d8c816b1e5d54f9cb965fca58"
    )
    assert synthetic_plan_digest(sparse) == (
        "f2a0e090629f1284b71f03a60109bd572d1f235386472241cfbd01d3749a4d27"
    )
    assert synthetic_plan_digest(
        plan_synthetic_corpus(
            documents=2_000,
            profile="synthetic",
            seed=260918,
        )
    ) != synthetic_plan_digest(synthetic)

    unrestricted = plan_synthetic_corpus(
        documents=1_000,
        profile="synthetic",
        seed=7,
    )
    restricted = plan_synthetic_corpus(
        documents=1_000,
        profile="synthetic",
        seed=7,
        restricted_library_pct=20,
    )
    assert unrestricted.context_folder_indices == restricted.context_folder_indices
    assert unrestricted.relations == restricted.relations
    assert unrestricted.body_links == restricted.body_links
    assert len(restricted.restricted_library_indices) == 20


def test_plan_digest_does_not_depend_on_python_hash_seed() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    command = [
        sys.executable,
        "-c",
        (
            "from backend.tests.utils.workspace_graph_synthetic import "
            "plan_synthetic_corpus, synthetic_plan_digest; "
            "print(synthetic_plan_digest(plan_synthetic_corpus("
            "documents=1000, profile='sparse', seed=41)))"
        ),
    ]
    digests = []
    for hash_seed in ("1", "2"):
        environment = child_env_without_dtxwiki_config()
        environment["PYTHONHASHSEED"] = hash_seed
        result = subprocess.run(
            command,
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        digests.append(result.stdout.strip())
    assert digests == [digests[0], digests[0]]


def test_schema_digest_includes_virtual_table_definition(tmp_path: Path) -> None:
    unicode_path = tmp_path / "unicode.sqlite"
    porter_path = tmp_path / "porter.sqlite"
    with closing(sqlite3.connect(unicode_path)) as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='unicode61')"
        )
    with closing(sqlite3.connect(porter_path)) as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='porter')"
        )

    assert sqlite_schema_digest(unicode_path) != sqlite_schema_digest(porter_path)
    assert any(
        record["type"] == "virtual_table_definition"
        and "unicode61" in str(record["sql"])
        for record in sqlite_schema_records(unicode_path)
    )

    binary_path = tmp_path / "binary-index.sqlite"
    nocase_path = tmp_path / "nocase-index.sqlite"
    with closing(sqlite3.connect(binary_path)) as connection:
        connection.execute("CREATE TABLE indexed_docs(title TEXT NOT NULL)")
        connection.execute(
            "CREATE INDEX indexed_docs_title_idx "
            "ON indexed_docs(title COLLATE BINARY DESC) WHERE title <> ''"
        )
    with closing(sqlite3.connect(nocase_path)) as connection:
        connection.execute("CREATE TABLE indexed_docs(title TEXT NOT NULL)")
        connection.execute(
            "CREATE INDEX indexed_docs_title_idx "
            "ON indexed_docs(title COLLATE NOCASE DESC) WHERE title <> ''"
        )
    assert sqlite_schema_digest(binary_path) != sqlite_schema_digest(nocase_path)


def test_git_fingerprint_can_exclude_owned_build_artifacts(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repository,
        check=True,
    )
    tracked = repository / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Graph Bench",
            "-c",
            "user.email=graph-bench@example.test",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        cwd=repository,
        check=True,
    )
    baseline = git_fingerprint(repository)
    artifact = repository / "corpus.sqlite"
    artifact.write_bytes(b"fixture\0database")

    assert git_fingerprint(repository)["worktree_sha256"] != baseline[
        "worktree_sha256"
    ]
    assert git_fingerprint(
        repository,
        excluded_paths=(artifact,),
    ) == baseline

    # Reproduce the relevant interleaving without touching the live checkout:
    # a second build creates its manifest between the first build's snapshots.
    # Excluding the first build's artifact must not hide the other build's file.
    other_manifest = repository / ".other-corpus.sqlite.build-test.manifest.json"
    other_manifest.write_bytes(b"{}")
    interleaved = git_fingerprint(repository, excluded_paths=(artifact,))
    assert interleaved["tracked_diff_sha256"] == baseline["tracked_diff_sha256"]
    assert interleaved["untracked_file_count"] == 1
    assert interleaved["worktree_sha256"] != baseline["worktree_sha256"]

    other_manifest.write_bytes(b'{"completed": true}')
    changed = git_fingerprint(repository, excluded_paths=(artifact,))
    assert changed["untracked_file_count"] == 1
    assert changed["worktree_sha256"] != interleaved["worktree_sha256"]
    other_manifest.unlink()
    assert git_fingerprint(repository, excluded_paths=(artifact,)) == baseline


def test_corpus_publication_removes_only_the_artifact_it_created(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.json"
    destination = tmp_path / "published.json"
    source.write_text("owned\n", encoding="utf-8")
    identity = build_corpus_script._publish_without_overwrite(
        source,
        destination,
    )
    assert destination.read_text(encoding="utf-8") == "owned\n"

    destination.unlink()
    destination.write_text("replacement\n", encoding="utf-8")
    assert not build_corpus_script._unlink_if_owned(destination, identity)
    assert destination.read_text(encoding="utf-8") == "replacement\n"

    second_source = tmp_path / "second-source.json"
    second_destination = tmp_path / "second-published.json"
    second_source.write_text("second\n", encoding="utf-8")
    second_identity = build_corpus_script._publish_without_overwrite(
        second_source,
        second_destination,
    )
    assert build_corpus_script._unlink_if_owned(
        second_destination,
        second_identity,
    )
    assert not second_destination.exists()


def test_synthetic_profiles_persist_deterministic_relation_endpoints() -> None:
    for profile in ("synthetic", "sparse"):
        first_corpora, first = _build_snapshot(
            documents=200,
            profile=profile,
            seed=20260917,
            workspaces=2,
            restricted_library_pct=20,
        )
        second_corpora, second = _build_snapshot(
            documents=200,
            profile=profile,
            seed=20260917,
            workspaces=2,
            restricted_library_pct=20,
        )

        assert first == second
        assert len(first_corpora) == len(second_corpora) == 2
        assert [corpus.document_ids for corpus in first_corpora] == [
            corpus.document_ids for corpus in second_corpora
        ]
        assert [corpus.relation_ids for corpus in first_corpora] == [
            corpus.relation_ids for corpus in second_corpora
        ]
        assert all(len(corpus.document_ids) == 200 for corpus in first_corpora)


def test_bulk_materializer_crosses_chunk_boundary_with_foreign_keys() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        flushes: list[None] = []

        @event.listens_for(session, "after_flush")
        def _record_flush(_session, _context) -> None:
            flushes.append(None)

        plan = plan_synthetic_corpus(
            documents=2_500,
            profile="synthetic",
            seed=20263417,
            corpus_key="scale-2500",
        )
        corpus = materialize_synthetic_corpus(session, plan)

        assert flushes == [None]
        assert list(session.execute(text("PRAGMA foreign_key_check"))) == []

        context_ids = set(corpus.context_ids)
        library_version_ids = set(corpus.library_version_ids)
        projections = session.execute(
            select(
                EdgeProjection.source_id,
                EdgeProjection.target_kind,
                EdgeProjection.target_id,
            ).where(EdgeProjection.workspace_id == corpus.workspace_id)
        ).all()
        assert all(source_id in context_ids for source_id, _, _ in projections)
        assert all(
            target_id
            in (
                context_ids
                if target_kind == "context_page"
                else library_version_ids
            )
            for _, target_kind, target_id in projections
        )

    engine.dispose()


def test_sparse_body_links_and_f05_degree_semantics_are_preserved() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        sparse = build_synthetic_corpus(
            session,
            documents=1_000,
            profile="sparse",
            seed=41,
        )[0]
        synthetic = build_synthetic_corpus(
            session,
            documents=1_000,
            profile="synthetic",
            seed=42,
        )[0]

        assert _context_degree_median(session, sparse) == 0
        assert _context_degree_median(session, synthetic) == 5

        sparse_bodies = session.scalars(
            select(ContextPageVersion.body_md).where(
                ContextPageVersion.workspace_id == sparse.workspace_id
            )
        ).all()
        assert sum("[[" in body for body in sparse_bodies) == 45
        assert sum("/context?target=" in body for body in sparse_bodies) == 135
        assert (
            session.scalar(
                select(func.count())
                .select_from(PageLink)
                .where(PageLink.workspace_id == sparse.workspace_id)
            )
            == 180
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(EdgeProjection)
                .where(
                    EdgeProjection.workspace_id == sparse.workspace_id,
                    EdgeProjection.edge_kind == "wikilink",
                )
            )
            == 180
        )

        first_wikilink = next(
            relation
            for relation in plan_synthetic_corpus(
                documents=1_000,
                profile="sparse",
                seed=41,
            ).relations
            if relation.link_syntax == "wikilink"
        )
        page = session.get(ContextPage, sparse.context_ids[first_wikilink.source_index])
        version = session.get(
            ContextPageVersion,
            sparse.context_version_ids[first_wikilink.source_index],
        )
        assert page is not None and version is not None
        sync_page_projections(
            session,
            page=page,
            version=version,
            record_graph_observations=False,
        )
        session.flush()
        assert (
            session.scalar(
                select(func.count())
                .select_from(PageLink)
                .where(
                    PageLink.source_page_id == page.id,
                    PageLink.source_version_id == version.id,
                )
            )
            == 1
        )

        sparse_plan = plan_synthetic_corpus(
            documents=1_000,
            profile="sparse",
            seed=41,
        )
        wikilink_sources = {
            relation.source_index
            for relation in sparse_plan.relations
            if relation.link_syntax == "wikilink"
        }
        url_only = next(
            link
            for link in sparse_plan.body_links
            if link.syntax == "url" and link.source_index not in wikilink_sources
        )
        url_page = session.get(ContextPage, sparse.context_ids[url_only.source_index])
        url_version = session.get(
            ContextPageVersion,
            sparse.context_version_ids[url_only.source_index],
        )
        assert url_page is not None and url_version is not None
        sync_page_projections(
            session,
            page=url_page,
            version=url_version,
            record_graph_observations=False,
        )
        session.flush()
        assert (
            session.scalar(
                select(func.count())
                .select_from(PageLink)
                .where(
                    PageLink.source_page_id == url_page.id,
                    PageLink.source_version_id == url_version.id,
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(EdgeProjection)
                .where(
                    EdgeProjection.source_id == url_page.id,
                    EdgeProjection.edge_kind == "wikilink",
                )
            )
            == 1
        )

    engine.dispose()


def test_restricted_ratio_and_tenant_isolation_are_exact() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        corpora = build_synthetic_corpus(
            session,
            documents=1_000,
            profile="synthetic",
            seed=7,
            workspaces=3,
            restricted_library_pct=20,
        )

        assert len(corpora) == 3
        for corpus in corpora:
            assert len(corpus.library_ids) == 100
            assert len(corpus.restricted_library_ids) == 20
            persisted = session.scalars(
                select(LibraryItem.id).where(
                    LibraryItem.workspace_id == corpus.workspace_id,
                    LibraryItem.trust_level == "restricted",
                )
            ).all()
            assert set(persisted) == set(corpus.restricted_library_ids)

            relations = session.scalars(
                select(EdgeProjection).where(
                    EdgeProjection.workspace_id == corpus.workspace_id
                )
            ).all()
            own_context = set(corpus.context_ids)
            own_targets = {
                *corpus.context_ids,
                *corpus.library_ids,
                *corpus.library_version_ids,
            }
            assert all(relation.source_id in own_context for relation in relations)
            assert all(relation.target_id in own_targets for relation in relations)

    engine.dispose()


def test_sparse_materialization_is_a_full_reprojection_fixed_point() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        corpus = build_synthetic_corpus(
            session,
            documents=200,
            profile="sparse",
            seed=41,
        )[0]

        def link_rows() -> set[tuple[object, ...]]:
            return set(
                session.execute(
                    select(
                        PageLink.source_page_id,
                        PageLink.source_version_id,
                        PageLink.link_syntax,
                        PageLink.target_slug,
                        PageLink.target_page_id,
                        PageLink.occurrence_count,
                    ).where(PageLink.workspace_id == corpus.workspace_id)
                ).all()
            )

        def edge_rows() -> set[tuple[object, ...]]:
            return set(
                session.execute(
                    select(
                        EdgeProjection.source_id,
                        EdgeProjection.target_kind,
                        EdgeProjection.target_id,
                        EdgeProjection.edge_kind,
                    ).where(EdgeProjection.workspace_id == corpus.workspace_id)
                ).all()
            )

        expected_links = link_rows()
        expected_edges = edge_rows()
        for page_id, version_id in zip(
            corpus.context_ids,
            corpus.context_version_ids,
            strict=True,
        ):
            page = session.get(ContextPage, page_id)
            version = session.get(ContextPageVersion, version_id)
            assert page is not None and version is not None
            sync_page_projections(
                session,
                page=page,
                version=version,
                record_graph_observations=False,
            )
        session.flush()

        assert link_rows() == expected_links
        assert edge_rows() == expected_edges

    engine.dispose()


def test_benchmark_principals_are_real_members_without_unsupported_tokens() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        corpus = build_synthetic_corpus(
            session,
            documents=40,
            profile="sparse",
            seed=11,
        )[0]
        principals = seed_benchmark_principals(session, corpus)

        assert [principal.name for principal in principals] == [
            "owner",
            "viewer",
            "restricted_reader",
        ]
        users = session.scalars(select(User)).all()
        assert len(users) == 3
        assert all(user.created_at == fixture_time() for user in users)
        assert all(user.updated_at == fixture_time() for user in users)
        assert all(
            CredentialHasher().verify_password(
                BENCHMARK_PASSWORD,
                user.password_hash,
            )
            for user in users
        )
        memberships = session.scalars(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == corpus.workspace_id
            )
        ).all()
        assert sorted(membership.role for membership in memberships) == [
            "owner",
            "viewer",
            "viewer",
        ]
        assert session.scalars(select(ApiToken)).all() == []
        workspace = session.get(Workspace, corpus.workspace_id)
        assert workspace is not None
        assert workspace.owner_user_id == principals[0].user_id
        actors = {
            principal.name: benchmark_actor(corpus, principal)
            for principal in principals
        }
        assert actor_can_read_restricted_library(actors["owner"])
        assert not actor_can_read_restricted_library(actors["viewer"])
        assert actor_can_read_restricted_library(actors["restricted_reader"])
        assert actors["restricted_reader"].token_workspace_ids == (
            corpus.workspace_id,
        )

    engine.dispose()


def test_live_checkout_corpus_cli_cases_share_one_xdist_group() -> None:
    # Discover both the established CLI test names and any new case that uses
    # the real entrypoint directly, so another checkout writer is not silently
    # added without joining the same source-fingerprint scheduling group.
    cases = {
        name: case
        for name, case in globals().items()
        if isinstance(case, FunctionType)
        and name.startswith("test_")
        and (
            name.startswith("test_build_corpus_cli_")
            or _CORPUS_CLI_PATH in case.__code__.co_consts
        )
    }
    assert len(cases) >= 3
    _assert_corpus_cli_source_group(cases)


@pytest.mark.parametrize(
    "groups",
    [(), ("different-group",), (_CORPUS_CLI_SOURCE_GROUP, "extra-group")],
    ids=["missing", "different", "combined"],
)
def test_live_checkout_corpus_cli_group_guard_rejects_parallel_cases(
    groups: tuple[str, ...],
) -> None:
    def new_cli_case() -> None:
        pass

    for group in groups:
        pytest.mark.xdist_group(group)(new_cli_case)
    with pytest.raises(AssertionError, match="must have exactly one xdist_group"):
        _assert_corpus_cli_source_group({"new_cli_case": new_cli_case})


def test_live_checkout_corpus_cli_group_guard_accepts_keyword_group() -> None:
    @pytest.mark.integration
    @pytest.mark.xdist_group(name=_CORPUS_CLI_SOURCE_GROUP)
    def new_cli_case() -> None:
        pass

    _assert_corpus_cli_source_group({"new_cli_case": new_cli_case})


# These cases fingerprint the same checkout while the repository-output case
# creates/removes untracked manifests. Only their own artifacts are excluded by
# the CLI, so they must never run on separate xdist workers at the same time.
@pytest.mark.xdist_group(_CORPUS_CLI_SOURCE_GROUP)
def test_build_corpus_cli_migrates_and_writes_verified_manifest(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "corpus.sqlite"
    manifest_path = Path(f"{database_path}.manifest.json")
    repository_root = Path(__file__).resolve().parents[3]
    command = [
        sys.executable,
        str(repository_root / "scripts/bench/graph/build_corpus.py"),
        "--documents",
        "40",
        "--profile",
        "synthetic",
        "--workspaces",
        "2",
        "--restricted-library-pct",
        "25",
        "--relation-multiplier",
        "1",
        "--seed",
        "11",
        "--out",
        str(database_path),
    ]
    environment = child_env_without_dtxwiki_config()
    environment["DTXWIKI_APP_MODE"] = "invalid-benchmark-poison"
    environment["PYTHONHASHSEED"] = "0"
    result = subprocess.run(
        command,
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert database_path.is_file()
    assert manifest_path.is_file()
    summary = json.loads(result.stdout)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert summary["manifest"] == str(manifest_path)
    assert manifest["generator_version"] == SYNTHETIC_GENERATOR_VERSION
    assert manifest["application"]["alembic_head"] == current_alembic_head()
    assert manifest["application"]["alembic_revision"] == current_alembic_head()
    assert manifest["application"]["version"] == source_version()
    assert manifest["arguments"]["relation_multiplier"] == 1
    assert manifest["runtime"]["python_hash_seed"] == "0"
    assert manifest["database"]["path"] == database_path.name
    assert manifest["database"]["sha256"] == file_sha256(database_path)
    assert manifest["database"]["schema_sha256"] == sqlite_schema_digest(
        database_path
    )
    assert manifest["database"]["content_sha256"] == sqlite_content_digest(
        database_path
    )
    assert len(manifest["workspaces"]) == 2
    assert [workspace["corpus_key"] for workspace in manifest["workspaces"]] == [
        "scale-40",
        "scale-40-workspace-1",
    ]
    assert all(
        workspace["relation_multiplier"] == 1
        for workspace in manifest["workspaces"]
    )
    assert all(
        workspace["row_counts"]["context_page"] == 36
        and workspace["row_counts"]["context_page_version"] == 36
        and workspace["row_counts"]["library_item"] == 4
        and workspace["row_counts"]["library_item_version"] == 4
        and workspace["row_counts"]["workspace_tree_node"] == 45
        and workspace["row_counts"]["workspace_memberships"] == 3
        and workspace["row_counts"]["edge_projection"]
        == workspace["relations"]
        for workspace in manifest["workspaces"]
    )
    for table_name in (
        "context_page",
        "context_page_version",
        "library_item",
        "library_item_version",
        "workspace_tree_node",
        "workspace_memberships",
        "edge_projection",
    ):
        assert sum(
            workspace["row_counts"][table_name]
            for workspace in manifest["workspaces"]
        ) == manifest["database"]["row_counts"][table_name]
    assert manifest["database"]["validation"] == {
        "foreign_key_violation_count": 0,
        "integrity_check": "ok",
    }
    assert all(
        [principal["name"] for principal in workspace["principals"]]
        == ["owner", "viewer", "restricted_reader"]
        for workspace in manifest["workspaces"]
    )

    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
        app_version=source_version(),
        auto_bootstrap=False,
    )
    assert migration_status(settings).status == "synced"
    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    with Session(engine) as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == (
            current_alembic_head()
        )
        version = session.get(AppMetadata, "dtxwiki_version")
        assert version is not None and version.value == source_version()
        assert len(session.scalars(select(Workspace)).all()) == 2
        assert len(session.scalars(select(User)).all()) == 6
        assert len(session.scalars(select(WorkspaceMembership)).all()) == 6
        assert session.scalars(select(ApiToken)).all() == []
    engine.dispose()

    template_path = tmp_path / "template.sqlite"
    template_settings = Settings(
        database_url=f"sqlite+pysqlite:///{template_path.as_posix()}",
        app_version=source_version(),
        auto_bootstrap=False,
    )
    ensure_app_version_synced(template_settings)
    assert sqlite_schema_digest(template_path) == sqlite_schema_digest(database_path)

    repeated_path = tmp_path / "corpus-repeated.sqlite"
    repeated_command = [*command[:-1], str(repeated_path)]
    repeated = subprocess.run(
        repeated_command,
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert sqlite_schema_digest(repeated_path) == sqlite_schema_digest(database_path)
    assert sqlite_content_digest(repeated_path) == sqlite_content_digest(database_path)

    original_sha256 = hashlib.sha256(database_path.read_bytes()).hexdigest()
    refused = subprocess.run(
        command,
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "refusing to replace existing output" in refused.stderr
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == original_sha256


@pytest.mark.xdist_group(_CORPUS_CLI_SOURCE_GROUP)
def test_build_corpus_cli_records_high_density_and_fails_fast(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    script = str(repository_root / "scripts/bench/graph/build_corpus.py")
    dense_path = tmp_path / "dense.sqlite"
    environment = child_env_without_dtxwiki_config()
    environment["PYTHONHASHSEED"] = "0"
    result = subprocess.run(
        [
            sys.executable,
            script,
            "--documents",
            "40",
            "--profile",
            "synthetic",
            "--relation-multiplier",
            "4",
            "--seed",
            "11",
            "--out",
            str(dense_path),
        ],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(
        Path(f"{dense_path}.manifest.json").read_text(encoding="utf-8")
    )
    base = plan_synthetic_corpus(
        documents=40,
        profile="synthetic",
        seed=11,
        corpus_key="scale-40",
    )
    workspace = manifest["workspaces"][0]
    assert manifest["arguments"]["relation_multiplier"] == 4
    assert workspace["corpus_key"] == "scale-40-density-4x"
    assert workspace["relation_multiplier"] == 4
    assert workspace["relation_kinds"] == {
        edge_kind: count * 4
        for edge_kind, count in base.relation_counts.items()
    }
    assert workspace["row_counts"]["edge_projection"] == sum(
        workspace["relation_kinds"].values()
    )

    invalid_path = tmp_path / "invalid.sqlite"
    invalid = subprocess.run(
        [
            sys.executable,
            script,
            "--documents",
            "9",
            "--profile",
            "synthetic",
            "--seed",
            "11",
            "--out",
            str(invalid_path),
        ],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0
    assert "documents must be at least 10" in invalid.stderr
    assert "alembic.runtime.migration" not in invalid.stderr
    assert not invalid_path.exists()


@pytest.mark.xdist_group(_CORPUS_CLI_SOURCE_GROUP)
def test_build_corpus_cli_excludes_its_untracked_repository_output() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    database_path = repository_root / f"graph-bench-test-{uuid4().hex}.sqlite"
    manifest_path = Path(f"{database_path}.manifest.json")
    environment = child_env_without_dtxwiki_config()
    environment["PYTHONHASHSEED"] = "0"
    command = [
        sys.executable,
        str(repository_root / "scripts/bench/graph/build_corpus.py"),
        "--documents",
        "10",
        "--profile",
        "sparse",
        "--seed",
        "17",
        "--out",
        str(database_path),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert database_path.is_file()
        assert manifest_path.is_file()
    finally:
        for path in (
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
            Path(f"{database_path}-journal"),
            manifest_path,
        ):
            path.unlink(missing_ok=True)
