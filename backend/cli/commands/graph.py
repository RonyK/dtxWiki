"""Operator maintenance commands for graph projections."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID

import typer
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from backend.app.container import DatabaseProvider
from backend.app.db import make_session_factory_from_settings
from backend.app.settings import Settings
from backend.models.workspace import Workspace
from backend.services.graph_density import relation_density_report
from backend.services.graph_reprojection import (
    WorkspaceGraphReprojectionResult,
    reproject_workspace_graph,
)

graph_app = typer.Typer(help="Inspect and repair workspace graph projections.")

_RETRYABLE_POSTGRES_CODES = frozenset({"23505", "40P01"})
_SQLSTATE = re.compile(r"[0-9A-Z]{5}")


@dataclass(frozen=True)
class _WorkspaceTarget:
    id: UUID
    slug: str


@graph_app.command(name="degree-report")
def graph_degree_report(
    workspace: str = typer.Option(
        ...,
        "--workspace",
        help="Workspace UUID or exact slug. UUID lookup takes precedence.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit stable graph-degree-report.v1 JSON.",
    ),
    db: str | None = typer.Option(None, help="Override database URL."),
    verbose_errors: bool = typer.Option(
        False,
        "--verbose-errors",
        help="Include full exception details in failure output.",
    ),
) -> None:
    """Report current Context relation density for operator diagnostics."""

    try:
        database_provider = _database_provider(db)
        targets = _select_targets(
            database_provider,
            workspace=workspace,
            all_workspaces=False,
        )
    except Exception as exc:
        _raise_degree_report_failure(
            scope=f"workspace_selector={workspace} stage=workspace_lookup",
            exc=exc,
            verbose_errors=verbose_errors,
        )
    if not targets:
        typer.echo(f"workspace not found: {workspace}", err=True)
        raise typer.Exit(code=1)

    target = targets[0]
    try:
        with database_provider.session_for(target.id)() as session:
            report = relation_density_report(session, target.id)
    except Exception as exc:
        _raise_degree_report_failure(
            scope=(
                f"workspace={target.slug} workspace_id={target.id} "
                "transaction=read_only"
            ),
            exc=exc,
            verbose_errors=verbose_errors,
        )

    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        typer.echo(
            "relation density "
            f"workspace={report.workspace_slug} workspace_id={report.workspace_id} "
            f"visibility_scope={report.visibility_scope} "
            f"context_pages={report.context_pages} "
            f"median_degree={_display_metric(report.median_degree)} "
            f"orphan_ratio={_display_metric(report.orphan_ratio)} "
            f"wikilink_edges={report.wikilink_edges} "
            f"citation_page_edges={report.citation_page_edges} "
            f"citation_source_edges={report.citation_source_edges} "
            f"library_items={report.library_items} "
            f"library_items_cited={report.library_items_cited} "
            f"gate_passed={str(report.gate_passed).lower()} "
            f"guidance_code={report.guidance_code} "
            "edge_projection_version="
            f"{report.edge_projection_version_before}->"
            f"{_display_metric(report.edge_projection_version_after)} "
            "edge_projection_version_stable="
            f"{str(report.edge_projection_version_stable).lower()}"
        )
    if not report.edge_projection_version_stable:
        typer.echo(
            "warning: edge projection changed while the density report was read; "
            "treat this result as a diagnostic sample",
            err=True,
        )


@graph_app.command(name="reproject")
def graph_reproject(
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        help="Workspace UUID or exact slug. UUID lookup takes precedence.",
    ),
    all_workspaces: bool = typer.Option(
        False,
        "--all-workspaces",
        help="Reproject every workspace in stable slug order.",
    ),
    db: str | None = typer.Option(None, help="Override database URL."),
    verbose_errors: bool = typer.Option(
        False,
        "--verbose-errors",
        help="Include full exception details in failure output.",
    ),
) -> None:
    """Repair PageLink and authored-link edges from live Context versions."""

    if (workspace is None and not all_workspaces) or (
        workspace is not None and all_workspaces
    ):
        raise typer.BadParameter(
            "provide exactly one of --workspace or --all-workspaces"
        )

    database_provider = _database_provider(db)
    targets = _select_targets(
        database_provider,
        workspace=workspace,
        all_workspaces=all_workspaces,
    )
    if not targets:
        description = (
            f"workspace not found: {workspace}"
            if workspace is not None
            else "no workspaces found"
        )
        typer.echo(description, err=True)
        raise typer.Exit(code=1)

    totals = {
        "pages": 0,
        "versions": 0,
        "page_links": 0,
        "wikilink_edges": 0,
        "page_links_inserted": 0,
        "page_links_updated": 0,
        "page_links_deleted": 0,
        "edges_inserted": 0,
        "edges_updated": 0,
        "edges_deleted": 0,
    }
    failures: list[_WorkspaceTarget] = []
    for target in targets:
        report = _reproject_target(
            database_provider,
            target,
            verbose_errors=verbose_errors,
        )
        if report is None:
            failures.append(target)
            continue
        totals["pages"] += report.pages
        totals["versions"] += report.versions
        totals["page_links"] += report.page_links
        totals["wikilink_edges"] += report.wikilink_edges
        totals["page_links_inserted"] += report.page_link_changes.inserted
        totals["page_links_updated"] += report.page_link_changes.updated
        totals["page_links_deleted"] += report.page_link_changes.deleted
        totals["edges_inserted"] += report.edge_changes.inserted
        totals["edges_updated"] += report.edge_changes.updated
        totals["edges_deleted"] += report.edge_changes.deleted
        typer.echo(
            "reprojected "
            f"workspace={target.slug} workspace_id={target.id} "
            f"pages={report.pages} versions={report.versions} "
            f"page_links={report.page_links} "
            f"wikilink_edges={report.wikilink_edges} "
            "page_link_changes="
            f"+{report.page_link_changes.inserted}/"
            f"~{report.page_link_changes.updated}/"
            f"-{report.page_link_changes.deleted} "
            "edge_changes="
            f"+{report.edge_changes.inserted}/"
            f"~{report.edge_changes.updated}/"
            f"-{report.edge_changes.deleted} "
            "edge_projection_version="
            f"{report.edge_projection_version_before}->"
            f"{report.edge_projection_version_after} "
            f"ignored_non_live_pointers={report.ignored_non_live_pointers}"
        )

    typer.echo(
        "summary: "
        f"selected={len(targets)} succeeded={len(targets) - len(failures)} "
        f"failed={len(failures)} pages={totals['pages']} "
        f"versions={totals['versions']} page_links={totals['page_links']} "
        f"wikilink_edges={totals['wikilink_edges']} "
        "page_link_changes="
        f"+{totals['page_links_inserted']}/~{totals['page_links_updated']}/"
        f"-{totals['page_links_deleted']} "
        "edge_changes="
        f"+{totals['edges_inserted']}/~{totals['edges_updated']}/"
        f"-{totals['edges_deleted']}"
    )
    if failures:
        raise typer.Exit(code=1)


def _select_targets(
    database_provider: DatabaseProvider,
    *,
    workspace: str | None,
    all_workspaces: bool,
) -> list[_WorkspaceTarget]:
    with database_provider.control_session_factory()() as session:
        if all_workspaces:
            rows = session.execute(
                select(Workspace.id, Workspace.slug).order_by(
                    Workspace.slug.asc(),
                    Workspace.id.asc(),
                )
            ).all()
        else:
            assert workspace is not None
            workspace_id = _parse_uuid(workspace)
            row = (
                session.execute(
                    select(Workspace.id, Workspace.slug).where(
                        Workspace.id == workspace_id
                    )
                ).one_or_none()
                if workspace_id is not None
                else None
            )
            if row is None:
                row = session.execute(
                    select(Workspace.id, Workspace.slug).where(
                        Workspace.slug == workspace
                    )
                ).one_or_none()
            rows = [] if row is None else [row]
    return [_WorkspaceTarget(id=row.id, slug=row.slug) for row in rows]


def _reproject_target(
    database_provider: DatabaseProvider,
    target: _WorkspaceTarget,
    *,
    verbose_errors: bool,
) -> WorkspaceGraphReprojectionResult | None:
    for attempt in (1, 2):
        try:
            with database_provider.session_for(target.id).begin() as session:
                return reproject_workspace_graph(session, target.id)
        except Exception as exc:  # isolate one workspace and continue
            postgres_code = _postgres_error_code(exc)
            if attempt == 1 and postgres_code in _RETRYABLE_POSTGRES_CODES:
                typer.echo(
                    "retrying "
                    f"workspace={target.slug} workspace_id={target.id} "
                    f"code={postgres_code} attempt=2/2",
                    err=True,
                )
                continue
            error_code = getattr(exc, "error_code", postgres_code or "reproject_failed")
            detail = (
                f": {exc}"
                if verbose_errors or hasattr(exc, "error_code")
                else f" error_type={type(exc).__name__}"
            )
            typer.echo(
                "failed "
                f"workspace={target.slug} workspace_id={target.id} "
                f"code={error_code} transaction=rolled_back{detail}",
                err=True,
            )
            return None
    return None


def _postgres_error_code(exc: Exception) -> str | None:
    """Return the SQLSTATE of a database driver error, else ``None``.

    Only the driver exception that SQLAlchemy wraps is consulted, so an
    application error that happens to carry a similar mapping is never a retry
    signal. psycopg exposes ``sqlstate``/``pgcode``; pg8000 carries the server
    fields as its first argument, with the SQLSTATE under ``C``. A value that is
    not a five-character SQLSTATE is ignored rather than echoed as one.
    """

    if not isinstance(exc, DBAPIError):
        return None
    driver_error = exc.orig
    code = getattr(driver_error, "sqlstate", None) or getattr(
        driver_error, "pgcode", None
    )
    if code is None:
        args = getattr(driver_error, "args", ())
        if args and isinstance(args[0], Mapping):
            code = args[0].get("C")
    if isinstance(code, str) and _SQLSTATE.fullmatch(code):
        return code
    return None


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _database_provider(db: str | None) -> DatabaseProvider:
    settings = Settings(database_url=db) if db else Settings()
    session_factory = make_session_factory_from_settings(settings)
    return DatabaseProvider(settings, session_factory=session_factory)


def _display_metric(value: object | None) -> str:
    return "null" if value is None else str(value)


def _raise_degree_report_failure(
    *,
    scope: str,
    exc: Exception,
    verbose_errors: bool,
) -> NoReturn:
    detail = f": {exc}" if verbose_errors else f" error_type={type(exc).__name__}"
    typer.echo(f"degree report failed {scope}{detail}", err=True)
    raise typer.Exit(code=1) from exc
