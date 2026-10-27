"""Pre-upgrade input audits and explicit metadata repairs."""

import json
from uuid import UUID

import typer
from sqlalchemy.exc import SQLAlchemyError

from backend.app.settings import Settings
from backend.cli.commands.admin import cli_operator_actor_id, users_app
from backend.services.ops.input_maintenance import audit_inputs, repair_input

inputs_app = typer.Typer(
    help="Audit input policy and repair explicitly selected metadata."
)


def _url(db):
    return (Settings(database_url=db) if db else Settings()).database_url


def _repair(entity, identity, field, value, db):
    try:
        repair_input(
            _url(db), entity, identity, field, value, actor_id=cli_operator_actor_id()
        )
    except (ValueError, RuntimeError, OSError) as exc:
        typer.echo(f"input repair refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    except SQLAlchemyError as exc:
        # Driver errors can include bound email/name parameters.
        typer.echo(
            "input repair failed: database conflict; no changes applied", err=True
        )
        raise typer.Exit(2) from exc
    typer.echo("Input repaired; database revision preserved.")


@inputs_app.command(name="audit")
def audit_command(db: str | None = typer.Option(None, help="Override database URL.")):
    """Read-only policy counts and bounded UUID/fingerprint examples."""
    try:
        report = audit_inputs(_url(db))
    except (ValueError, RuntimeError, OSError, SQLAlchemyError) as exc:
        typer.echo(
            "input audit refused: unsupported or inconsistent database", err=True
        )
        raise typer.Exit(2) from exc
    blocking = [item for item in report if item["classification"] == "blocking_scalar"]
    preserved = [item for item in report if item["classification"] != "blocking_scalar"]
    typer.echo(
        json.dumps({"blocking": blocking, "preserved": preserved}, ensure_ascii=True)
    )
    if blocking:
        raise typer.Exit(1)


@users_app.command(name="set-display-name")
def set_display_name(
    user_id: UUID,
    display_name: str = typer.Option(..., "--display-name"),
    db: str | None = typer.Option(None),
):
    """Repair a user by UUID without bootstrapping or upgrading."""
    _repair("user", user_id, "display_name", display_name, db)


@users_app.command(name="set-email")
def set_email(
    user_id: UUID,
    email: str = typer.Option(..., "--email"),
    db: str | None = typer.Option(None),
):
    """Change email and atomically invalidate prior recovery/session state."""
    _repair("user", user_id, "email", email, db)


@inputs_app.command(name="repair-metadata")
def repair_metadata(
    entity: str,
    identity: UUID,
    field: str = typer.Option(..., "--field"),
    value: str | None = typer.Option(None, "--value"),
    null_value: bool = typer.Option(
        False, "--null", help="Explicitly clear an optional note/summary."
    ),
    db: str | None = typer.Option(None),
):
    if (value is None) == (not null_value):
        typer.echo("provide exactly one of --value or --null", err=True)
        raise typer.Exit(2)
    _repair(entity, identity, field, value, db)
