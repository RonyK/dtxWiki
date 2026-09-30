"""Operator maintenance commands for Context history invariants."""

from __future__ import annotations

import typer
from sqlalchemy.exc import SQLAlchemyError

from backend.app.db import make_session_factory_from_settings
from backend.app.settings import Settings
from backend.repositories.context_page_repo import iter_version_block_maps
from backend.services.context_lifecycle import (
    ContextHistoryAuditReport,
    ContextHistoryRepairBlockedError,
    ContextHistoryRepairFailedError,
    audit_context_history,
    repair_context_history,
)
from backend.utils.block_identity import heal_block_map

context_app = typer.Typer(help="Inspect and repair Context operational state.")
context_history_app = typer.Typer(help="Inspect and repair Context version history.")


@context_history_app.command(name="repair")
def context_history_repair(
    apply: bool = typer.Option(
        False,
        "--apply/--dry-run",
        help=(
            "Apply unambiguous repairs. The safe default, --dry-run, only "
            "reports persisted invariant violations."
        ),
    ),
    db: str | None = typer.Option(None, help="Override database URL."),
) -> None:
    """Audit or repair legacy Context published-lineage inconsistencies."""

    settings = Settings(database_url=db) if db else Settings()
    # Do not bootstrap here: this command is the recovery path when the
    # Context-history preflight itself prevented an Alembic upgrade.
    session_factory = make_session_factory_from_settings(settings)

    if not apply:
        try:
            with session_factory() as session:
                report = audit_context_history(session)
        except SQLAlchemyError as exc:
            typer.echo(f"context history dry-run failed: {exc}", err=True)
            raise typer.Exit(2) from exc
        _render_report(report, mode="dry-run")
        typer.echo("No changes applied.")
        if report.violations:
            typer.echo(
                "Resolve blocking pointers, take a verified database backup, "
                "then re-run with --apply."
            )
            raise typer.Exit(1)
        return

    typer.echo(
        "WARNING: --apply mutates Context history. Take a verified database "
        "backup before continuing.",
        err=True,
    )
    try:
        with session_factory.begin() as session:
            result = repair_context_history(session)
    except ContextHistoryRepairBlockedError as exc:
        _render_report(exc.report, mode="apply-refused")
        typer.echo(
            "context history repair refused: one or more published pointers "
            "are missing or ambiguous; no changes were applied",
            err=True,
        )
        raise typer.Exit(2) from exc
    except ContextHistoryRepairFailedError as exc:
        _render_report(exc.report, mode="apply-rolled-back")
        typer.echo(
            "context history repair failed post-check; the transaction was "
            "rolled back",
            err=True,
        )
        raise typer.Exit(2) from exc
    except SQLAlchemyError as exc:
        typer.echo(f"context history repair failed: {exc}", err=True)
        raise typer.Exit(2) from exc

    _render_report(result.after, mode="apply")
    typer.echo(
        "Applied canonical Context history repair: "
        f"pages={result.repaired_pages} versions={result.repaired_versions}."
    )
    if result.unrepaired_pages:
        # The repair now excludes rather than blocks on an ambiguous page, so
        # a non-zero exit here is the only signal that a later upgrade
        # preflight will still refuse: reporting success (exit 0) would hide
        # that outstanding blocker from the operator and from scripts.
        typer.echo(
            f"{result.unrepaired_pages} page(s) remain ambiguous and were not "
            "repaired; resolve them manually before retrying the upgrade.",
            err=True,
        )
        raise typer.Exit(1)


def _render_report(report: ContextHistoryAuditReport, *, mode: str) -> None:
    typer.echo(
        f"context history {mode}: pages={report.pages_scanned} "
        f"versions={report.versions_scanned} "
        f"violations={len(report.violations)} "
        f"repairable={report.repairable_violation_count} "
        f"blocking={report.blocking_violation_count}"
    )
    for violation in report.violations:
        typer.echo(
            f"- {violation.code} page={violation.page_id} "
            f"version={violation.version_id or '-'} "
            f"repairable={str(violation.repairable).lower()}"
        )


@context_app.command(name="block-map-audit")
def context_block_map_audit(
    db: str | None = typer.Option(None, help="Override database URL."),
) -> None:
    """Count versions whose stored block map no longer describes their body.

    Read-only. Before every in-place save kept the map in step, an edited row
    could carry a map for an earlier body; readers now rebuild such maps on
    the fly, and this reports how many exist, by version status.
    """

    settings = Settings(database_url=db) if db else Settings()
    session_factory = make_session_factory_from_settings(settings)
    totals: dict[str, dict[str, int]] = {}
    try:
        with session_factory() as session:
            for status, body_md, block_map in iter_version_block_maps(session):
                bucket = totals.setdefault(
                    status, {"total": 0, "missing": 0, "drifted": 0}
                )
                bucket["total"] += 1
                if block_map is None:
                    bucket["missing"] += 1
                    continue
                _map, repaired = heal_block_map(
                    body_md or "", block_map, identity_seed=None
                )
                if repaired:
                    bucket["drifted"] += 1
    except SQLAlchemyError as exc:
        typer.echo(f"block map audit failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    for status in sorted(totals):
        bucket = totals[status]
        typer.echo(
            f"status={status} total={bucket['total']} "
            f"drifted={bucket['drifted']} missing={bucket['missing']}"
        )
    typer.echo("No changes applied.")


context_app.add_typer(context_history_app, name="history")
