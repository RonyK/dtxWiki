"""`dtxwiki eval` — score retrieval modes and planner lanes (Epic-1 §13).

Retrieval-only metrics (recall@k, MRR, hit-rate) per retrieval mode. This is the
gate for ranking changes: only enable a reranker / query transform once the
numbers move here. Against a real embedding endpoint the semantic/hybrid rows are
meaningful; with no provider configured only the keyword row is produced.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy.orm import Session


def _golden_page_samples(
    session: Session,
    workspace_id: UUID,
    *,
    limit: int = 20,
) -> list[dict[str, object]]:
    from sqlalchemy import func, select

    from backend.models.context import ContextCitation, ContextPage

    return [
        {
            # page_id lets the lease-time builder re-resolve the sample and drop
            # or refresh it if the page is later archived/renamed, so a stale
            # title/path is never disclosed (Codex round-9).
            "page_id": str(page_id),
            "title": title,
            "concept_path": f"/context/{slug}.md",
            "citation_count": citation_count,
        }
        for page_id, title, slug, citation_count in session.execute(
            select(
                ContextPage.id,
                ContextPage.title,
                ContextPage.slug,
                func.count(ContextCitation.id),
            )
            .outerjoin(
                ContextCitation,
                ContextCitation.cited_context_page_version_id
                == ContextPage.current_published_version_id,
            )
            .where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.current_published_version_id.is_not(None),
                ContextPage.lifecycle_status != "archived",
            )
            .group_by(ContextPage.id)
            .order_by(func.count(ContextCitation.id).desc(), ContextPage.title)
            .limit(limit)
        ).all()
    ]


def eval_command(
    golden: Annotated[
        Path,
        typer.Option(
            "--golden",
            help=(
                "Golden JSON: [{query, expected:[...], "
                "lane?:entity|trigram|bm25|associative}]."
            ),
        ),
    ],
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Workspace slug (required if >1)."),
    ] = None,
    k: Annotated[
        int,
        typer.Option("--k", min=1, help="Cutoff for recall@k / hit-rate."),
    ] = 8,
    modes: Annotated[
        str | None,
        typer.Option("--modes", help="Comma-separated retrieval modes; default = all."),
    ] = None,
    db: Annotated[str | None, typer.Option(help="Override database URL.")] = None,
    baseline: Annotated[
        Path | None,
        typer.Option(
            "--baseline",
            help="Baseline JSON; exit 1 when a metric drops by more than 0.05.",
        ),
    ] = None,
    propose: Annotated[
        bool,
        typer.Option(
            "--propose",
            help=(
                "Queue a reviewed client-LLM task to propose additional golden queries."
            ),
        ),
    ] = False,
) -> None:
    """Evaluate retrieval quality of the configured backend over a golden set."""
    from sqlalchemy import select

    from backend.app.db import make_session_factory_from_settings
    from backend.app.settings import Settings
    from backend.models.workspace import Workspace
    from backend.services.eval.harness import (
        compare_baseline,
        evaluate_profile,
        load_golden,
        requires_associative_lane,
    )
    from backend.services.search_service import build_search_service

    settings = Settings(database_url=db) if db else Settings()

    try:
        golden_set = load_golden(json.loads(golden.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        typer.echo(f"failed to load golden set: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not golden_set:
        typer.echo("golden set is empty; nothing to evaluate.")
        return

    if requires_associative_lane(golden_set) and not settings.search_associative_lane:
        settings = settings.model_copy(update={"search_associative_lane": True})

    service = build_search_service(settings)
    supported = sorted(service.supported_modes())
    if modes:
        requested = [mode.strip() for mode in modes.split(",") if mode.strip()]
        unknown = [mode for mode in requested if mode not in supported]
        if unknown:
            typer.echo(
                f"unsupported mode(s): {', '.join(unknown)}; "
                f"supported = {', '.join(supported)}",
                err=True,
            )
            raise typer.Exit(code=1)
        selected = requested
    else:
        selected = supported

    session_factory = make_session_factory_from_settings(settings)
    with session_factory() as session:
        statement = select(Workspace.id, Workspace.slug)
        if workspace:
            statement = statement.where(Workspace.slug == workspace)
        targets = list(session.execute(statement).all())
        if not targets:
            typer.echo("no matching workspace.", err=True)
            raise typer.Exit(code=1)
        if len(targets) > 1:
            slugs = ", ".join(slug for _, slug in targets)
            typer.echo(
                f"multiple workspaces ({slugs}); pass --workspace to choose one.",
                err=True,
            )
            raise typer.Exit(code=1)
        workspace_id, workspace_slug = targets[0]
        results, lane_results = evaluate_profile(
            service,
            session,
            workspace_id,
            golden_set,
            modes=selected,
            k=k,
        )
        proposal_queued = False
        if propose:
            from backend.services.delegated_tasks import create_task

            existing_golden = [
                {
                    "query": item.query,
                    "expected": item.expected,
                    "lane": item.lane,
                }
                for item in golden_set
            ]
            page_samples = _golden_page_samples(session, workspace_id)
            proposal_payload = {
                "existing_golden": existing_golden[:50],
                "page_samples": page_samples,
                "k": k,
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    proposal_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            proposal_queued = (
                create_task(
                    session,
                    workspace_id=workspace_id,
                    task_type="golden_query_candidates",
                    target_kind="workspace",
                    target_id=workspace_id,
                    source="lint",
                    fingerprint=fingerprint,
                    payload=proposal_payload,
                    priority=90,
                )
                is not None
            )
            session.commit()

    typer.echo(
        f"workspace={workspace_slug}  queries={len(golden_set)}  k={k}  "
        f"provider={settings.embedding_provider or 'none'}"
    )
    if propose:
        typer.echo(
            "golden query proposal: "
            + ("queued" if proposal_queued else "already pending")
        )
    header = f"{'mode':<10} {'recall@k':>9} {'mrr':>7} {'hit_rate':>9}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for metric in results:
        typer.echo(
            f"{metric.mode:<10} {metric.recall_at_k:>9.3f} "
            f"{metric.mrr:>7.3f} {metric.hit_rate:>9.3f}"
        )
    typer.echo("")
    if not lane_results:
        typer.echo("lanes: no lane-tagged golden queries")
    else:
        lane_header = (
            f"{'lane':<10} {'queries':>7} {'recall@k':>9} {'mrr':>7} {'hit_rate':>9}"
        )
        typer.echo(lane_header)
        typer.echo("-" * len(lane_header))
        for metric in lane_results:
            typer.echo(
                f"{metric.lane:<10} {metric.queries:>7} "
                f"{metric.recall_at_k:>9.3f} {metric.mrr:>7.3f} "
                f"{metric.hit_rate:>9.3f}"
            )
    if baseline is not None:
        try:
            baseline_payload = json.loads(baseline.read_text(encoding="utf-8"))
            regressions = compare_baseline(results, lane_results, baseline_payload)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            typer.echo(f"failed to load eval baseline: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        if regressions:
            typer.echo("eval regression gate: FAILED", err=True)
            for regression in regressions:
                typer.echo(
                    f"  {regression.scope}.{regression.name}."
                    f"{regression.metric}: {regression.current:.3f} < "
                    f"{regression.baseline:.3f}",
                    err=True,
                )
            raise typer.Exit(code=1)
        typer.echo("eval regression gate: PASS")
