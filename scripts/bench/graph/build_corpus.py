#!/usr/bin/env python3
"""Build a deterministic SQLite corpus for Graph View benchmarks."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

ArtifactIdentity = tuple[int, int, int, int]

from sqlalchemy.orm import Session  # noqa: E402

import backend.models  # noqa: E402,F401
from backend.app.bootstrap import (  # noqa: E402
    current_alembic_head,
    ensure_app_version_synced,
    migration_status,
)
from backend.app.db import make_engine  # noqa: E402
from backend.app.settings import Settings  # noqa: E402
from backend.app.version import source_version  # noqa: E402
from backend.tests.utils.graph_bench_manifest import (  # noqa: E402
    build_corpus_manifest,
    git_fingerprint,
    write_corpus_manifest,
)
from backend.tests.utils.workspace_graph_corpora import (  # noqa: E402
    materialize_synthetic_corpus,
    seed_benchmark_principals,
)
from backend.tests.utils.workspace_graph_synthetic import (  # noqa: E402
    SYNTHETIC_GENERATOR_VERSION,
    plan_synthetic_corpus,
    synthetic_plan_digest,
    validate_synthetic_inputs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, required=True)
    parser.add_argument(
        "--profile",
        choices=("synthetic", "sparse"),
        required=True,
    )
    parser.add_argument("--workspaces", type=int, default=1)
    parser.add_argument("--restricted-library-pct", type=float, default=0)
    parser.add_argument(
        "--relation-multiplier",
        type=int,
        default=1,
        help=(
            "multiply each synthetic relation kind while preserving the "
            "historical relation prefix (high-density benchmark: 4)"
        ),
    )
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workspaces < 1:
        raise SystemExit("--workspaces must be at least 1")
    try:
        validate_synthetic_inputs(
            documents=args.documents,
            profile=args.profile,
            workspace_index=0,
            restricted_library_pct=args.restricted_library_pct,
            relation_multiplier=args.relation_multiplier,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    output_path = args.out.expanduser().resolve()
    manifest_path = Path(f"{output_path}.manifest.json")
    if output_path.exists() or manifest_path.exists():
        occupied = output_path if output_path.exists() else manifest_path
        raise SystemExit(f"refusing to replace existing output: {occupied}")
    initial_source_fingerprint = git_fingerprint(REPOSITORY_ROOT)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_database_path = _temporary_path(output_path, suffix=".sqlite")
    try:
        temporary_manifest_path = _temporary_path(
            output_path,
            suffix=".manifest.json",
        )
    except Exception:
        _remove_sqlite(temporary_database_path)
        raise
    database_url = f"sqlite+pysqlite:///{temporary_database_path.as_posix()}"
    # These three values are trusted local constants. model_construct avoids
    # reading unrelated DTXWIKI_* process configuration into a reproducible
    # offline corpus build.
    settings = Settings.model_construct(
        database_url=database_url,
        app_version=source_version(),
        auto_bootstrap=False,
    )
    engine = None
    completed = False
    published_database: ArtifactIdentity | None = None
    published_manifest: ArtifactIdentity | None = None
    try:
        ensure_app_version_synced(settings)
        status = migration_status(settings)
        if status.status != "synced" or status.alembic_db is None:
            raise RuntimeError(
                "benchmark corpus migration did not reach a synced application state"
            )
        engine = make_engine(database_url)
        workspace_summaries = []
        for workspace_index in range(args.workspaces):
            plan = plan_synthetic_corpus(
                documents=args.documents,
                profile=args.profile,
                seed=args.seed,
                workspace_index=workspace_index,
                restricted_library_pct=args.restricted_library_pct,
                relation_multiplier=args.relation_multiplier,
                corpus_key=_historical_corpus_key(args, workspace_index),
            )
            with Session(engine) as session, session.begin():
                corpus = materialize_synthetic_corpus(session, plan)
                principals = seed_benchmark_principals(session, corpus)
                workspace_summaries.append(
                    {
                        "id": str(corpus.workspace_id),
                        "corpus_key": corpus.name,
                        "plan_sha256": synthetic_plan_digest(plan),
                        "contexts": len(corpus.context_ids),
                        "library": len(corpus.library_ids),
                        "relations": len(corpus.relation_ids),
                        "relation_multiplier": plan.relation_multiplier,
                        "relation_kinds": plan.relation_counts,
                        "body_links": {
                            "wikilink": sum(
                                link.syntax == "wikilink" for link in plan.body_links
                            ),
                            "url": sum(
                                link.syntax == "url" for link in plan.body_links
                            ),
                        },
                        "restricted_library": len(corpus.restricted_library_ids),
                        "principals": [
                            {
                                "name": principal.name,
                                "user_id": str(principal.user_id),
                                "email": principal.email,
                                "role": principal.role,
                                "synthetic_scopes": list(principal.synthetic_scopes),
                            }
                            for principal in principals
                        ],
                    }
                )
            if args.documents >= 5_000:
                print(
                    f"built workspace {workspace_index + 1}/{args.workspaces}",
                    file=sys.stderr,
                    flush=True,
                )
        engine.dispose()
        engine = None
        status = migration_status(settings)
        if status.status != "synced" or status.alembic_db is None:
            raise RuntimeError(
                "benchmark corpus left the application migration state unsynced"
            )
        final_source_fingerprint = git_fingerprint(
            REPOSITORY_ROOT,
            excluded_paths=_temporary_artifact_paths(
                temporary_database_path,
                temporary_manifest_path,
            ),
        )
        if final_source_fingerprint != initial_source_fingerprint:
            raise RuntimeError("repository source changed during corpus generation")
        manifest = build_corpus_manifest(
            database_path=temporary_database_path,
            database_name=output_path.name,
            repository_root=REPOSITORY_ROOT,
            generator_version=SYNTHETIC_GENERATOR_VERSION,
            arguments={
                "documents": args.documents,
                "profile": args.profile,
                "workspaces": args.workspaces,
                "restricted_library_pct": args.restricted_library_pct,
                "relation_multiplier": args.relation_multiplier,
                "seed": args.seed,
            },
            app_version=settings.app_version,
            alembic_head=current_alembic_head(),
            alembic_revision=status.alembic_db,
            workspaces=workspace_summaries,
            source_fingerprint=initial_source_fingerprint,
        )
        write_corpus_manifest(temporary_manifest_path, manifest)
        try:
            # The database is the readiness marker. Publishing it last means
            # a crash can leave an inert manifest, never a DB that appears
            # complete without its verification record.
            published_manifest = _publish_without_overwrite(
                temporary_manifest_path,
                manifest_path,
            )
            published_database = _publish_without_overwrite(
                temporary_database_path,
                output_path,
            )
            if _artifact_identity(manifest_path) != published_manifest:
                raise RuntimeError(
                    "benchmark manifest changed during corpus publication"
                )
        except FileExistsError as exc:
            occupied = output_path if output_path.exists() else manifest_path
            raise SystemExit(
                f"refusing to replace existing output: {occupied}"
            ) from exc
        completed = True
        print(
            json.dumps(
                {
                    "database": str(output_path),
                    "manifest": str(manifest_path),
                    "documents_per_workspace": args.documents,
                    "profile": args.profile,
                    "relation_multiplier": args.relation_multiplier,
                    "seed": args.seed,
                    "workspaces": workspace_summaries,
                },
                ensure_ascii=False,
            )
        )
    finally:
        if engine is not None:
            engine.dispose()
        if not completed:
            if published_database is not None:
                _unlink_if_owned(output_path, published_database)
            if published_manifest is not None:
                _unlink_if_owned(manifest_path, published_manifest)
        _remove_sqlite(temporary_database_path)
        temporary_manifest_path.unlink(missing_ok=True)
        temporary_manifest_path.with_name(
            f".{temporary_manifest_path.name}.tmp"
        ).unlink(missing_ok=True)
    return 0


def _historical_corpus_key(
    args: argparse.Namespace,
    workspace_index: int,
) -> str | None:
    if args.profile != "synthetic":
        return None
    density_suffix = (
        "" if args.relation_multiplier == 1 else f"-density-{args.relation_multiplier}x"
    )
    workspace_suffix = "" if workspace_index == 0 else f"-workspace-{workspace_index}"
    return f"scale-{args.documents}{density_suffix}{workspace_suffix}"


def _temporary_path(output_path: Path, *, suffix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{output_path.name}.build-",
        suffix=suffix,
        dir=output_path.parent,
    )
    os.close(descriptor)
    return Path(raw_path)


def _remove_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _publish_without_overwrite(
    source: Path,
    destination: Path,
) -> ArtifactIdentity:
    """Atomically publish while preserving another process's destination."""

    if os.name == "nt":
        # Windows rename fails when the destination already exists and moves
        # the source, avoiding an undeletable extra hard link to an open DB.
        source.rename(destination)
    else:
        os.link(source, destination)
    return _artifact_identity(destination)


def _artifact_identity(path: Path) -> ArtifactIdentity:
    status = path.stat()
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_size),
        int(status.st_mtime_ns),
    )


def _unlink_if_owned(path: Path, identity: ArtifactIdentity) -> bool:
    try:
        if _artifact_identity(path) != identity:
            return False
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _temporary_artifact_paths(
    database_path: Path,
    manifest_path: Path,
) -> tuple[Path, ...]:
    return (
        *(
            Path(f"{database_path}{suffix}")
            for suffix in ("", "-wal", "-shm", "-journal")
        ),
        manifest_path,
        manifest_path.with_name(f".{manifest_path.name}.tmp"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
