"""Add durable scope/version grants for anonymous Context pages.

Legacy ``is_public`` rows are safe to retain only when they belong to a public
Workspace and point at that page's real current published snapshot. Those rows
become ``indexed_web`` grants. Every non-public, archived, headless, cross-page,
or otherwise invalid legacy flag is revoked instead of being guessed.
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from alembic import op

from backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema import (
    unique_key_prepass,
)

_SCAFFOLDING_PREFIX = "_dtxwiki_0002_"

# The cycle-breaking parent keys of the context_page <-> context_page_version
# pair, in one place: the prepass, the retire pass, the batches that would
# otherwise create them a second time, and the downgrade all read this table.
# ``temp_index`` is the SQLite-only scaffolding name; ``constraint`` is the
# durable key that exists in the finished schema either way. See
# ``unique_key_prepass`` for why the two dialects need opposite shapes.
_PARENT_UNIQUE_KEYS = (
    (
        "context_page",
        "uq_context_page_workspace_id",
        ("workspace_id", "id"),
        _SCAFFOLDING_PREFIX + "context_page_workspace_id",
    ),
    (
        "context_page_version",
        "uq_context_page_version_page_id",
        ("page_id", "id"),
        _SCAFFOLDING_PREFIX + "context_version_page_id",
    ),
    (
        "context_page_version",
        "uq_context_page_version_workspace_id",
        ("workspace_id", "id"),
        _SCAFFOLDING_PREFIX + "context_version_workspace_id",
    ),
    (
        "context_page_version",
        "uq_context_page_version_workspace_page_owner",
        ("workspace_id", "page_id", "id"),
        _SCAFFOLDING_PREFIX + "context_version_workspace_page_owner",
    ),
)
_OLD_VERSION_STATUSES = ("draft", "published_snapshot", "superseded_snapshot")
_NEW_VERSION_STATUSES = (
    "editing",
    "draft",
    "published_snapshot",
    "superseded_snapshot",
    "discarded",
)


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    connection = op.get_bind()
    _add_context_columns()
    _backfill_version_workspaces(connection)
    _apply_public_exposure_grants(connection)
    _publish_owner_unique_keys()

    # context_page and context_page_version are a reciprocal FK pair. The
    # temporary unique indexes make both parent keys available so each table
    # can be rebuilt exactly once into its complete final shape.
    with op.batch_alter_table("context_page_version") as batch_op:
        batch_op.drop_constraint(
            "fk_context_page_version_page_id_context_page",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_context_page_version_parent_version_id_context_p_2d550ab1",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "context_page_version_status_check",
            type_="check",
        )
        batch_op.drop_column("search_doc")
        batch_op.alter_column("workspace_id", nullable=False)
        batch_op.add_column(
            sa.Column(
                "last_saved_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        unique_key_prepass.create_unique_constraint_once(
            batch_op,
            "uq_context_page_version_page_id",
            ["page_id", "id"],
            already_published=True,
        )
        unique_key_prepass.create_unique_constraint_once(
            batch_op,
            "uq_context_page_version_workspace_id",
            ["workspace_id", "id"],
            already_published=True,
        )
        unique_key_prepass.create_unique_constraint_once(
            batch_op,
            "uq_context_page_version_workspace_page_owner",
            ["workspace_id", "page_id", "id"],
            already_published=True,
        )
        batch_op.create_foreign_key(
            "fk_context_page_version_page_id_workspace_scoped",
            "context_page",
            ["workspace_id", "page_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_foreign_key(
            "fk_context_page_version_parent_version_id_workspace_scoped",
            "context_page_version",
            ["workspace_id", "parent_version_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_foreign_key(
            "fk_context_page_version_merged_into_version_id_workspace_scoped",
            "context_page_version",
            ["workspace_id", "merged_into_version_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_foreign_key(
            "fk_context_page_version_workspace_id",
            "workspace",
            ["workspace_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "context_page_version_status_check",
            f"version_status IN ({_sql_in(_NEW_VERSION_STATUSES)})",
        )

    with op.batch_alter_table("context_page") as batch_op:
        batch_op.drop_constraint(
            "fk_context_page_current_published_version_id_contex_7cb72200",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_context_page_latest_draft_version_id_context_page_version",
            type_="foreignkey",
        )
        batch_op.drop_column("created_from_ask_turn_ids")
        unique_key_prepass.create_unique_constraint_once(
            batch_op,
            "uq_context_page_workspace_id",
            ["workspace_id", "id"],
            already_published=True,
        )
        batch_op.create_foreign_key(
            "fk_context_page_id_workspace_scoped",
            "context_page_version",
            ["workspace_id", "id", "public_exposed_version_id"],
            ["workspace_id", "page_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_context_page_current_published_version_id_workspace_scoped",
            "context_page_version",
            ["workspace_id", "current_published_version_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_foreign_key(
            "fk_context_page_latest_draft_version_id_workspace_scoped",
            "context_page_version",
            ["workspace_id", "latest_draft_version_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_check_constraint(
            "context_page_public_exposure_scope_check",
            "public_exposure_scope IS NULL OR "
            "public_exposure_scope IN ('indexed_web', 'unlisted_web')",
        )
        batch_op.create_check_constraint(
            "context_page_public_exposure_complete_check",
            "((NOT is_public AND public_exposure_scope IS NULL "
            "AND public_exposed_version_id IS NULL) OR "
            "(is_public AND public_exposure_scope IS NOT NULL "
            "AND public_exposed_version_id IS NOT NULL))",
        )

    _retire_owner_unique_keys()


def downgrade() -> None:
    # Sequenced by versions/v0_1/efcbcf724889_core_schema.py, the single owner of
    # prepare-step
    # order across modules; re-calling it here would be a second source.
    with op.batch_alter_table("context_page") as batch_op:
        batch_op.drop_constraint(
            "context_page_public_exposure_complete_check",
            type_="check",
        )
        batch_op.drop_constraint(
            "context_page_public_exposure_scope_check",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_context_page_id_workspace_scoped",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_context_page_current_published_version_id_workspace_scoped",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_context_page_latest_draft_version_id_workspace_scoped",
            type_="foreignkey",
        )
        unique_key_prepass.drop_unique_constraint_once(
            batch_op, "uq_context_page_workspace_id", defer_to_retract=True
        )
        batch_op.create_foreign_key(
            "fk_context_page_current_published_version_id_contex_7cb72200",
            "context_page_version",
            ["current_published_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_context_page_latest_draft_version_id_context_page_version",
            "context_page_version",
            ["latest_draft_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.drop_column("public_exposed_version_id")
        batch_op.drop_column("public_exposure_scope")
        batch_op.drop_column("stale_candidate")
        batch_op.alter_column(
            "created_from_ask_turn_ids",
            existing_type=sa.JSON(),
            nullable=False,
        )
    with op.batch_alter_table("context_page_version") as batch_op:
        batch_op.drop_constraint(
            "context_page_version_status_check",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_context_page_version_workspace_id",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_context_page_version_merged_into_version_id_workspace_scoped",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_context_page_version_parent_version_id_workspace_scoped",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_context_page_version_page_id_workspace_scoped",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_context_page_version_workspace_page_owner", type_="unique"
        )
        batch_op.drop_constraint("uq_context_page_version_workspace_id", type_="unique")
        batch_op.drop_constraint("uq_context_page_version_page_id", type_="unique")
        batch_op.create_foreign_key(
            "fk_context_page_version_page_id_context_page",
            "context_page",
            ["page_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_context_page_version_parent_version_id_context_p_2d550ab1",
            "context_page_version",
            ["parent_version_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "context_page_version_status_check",
            f"version_status IN ({_sql_in(_OLD_VERSION_STATUSES)})",
        )
        batch_op.add_column(sa.Column("search_doc", sa.Text(), nullable=True))
        batch_op.drop_column("last_saved_at")
        batch_op.drop_column("editing_revision")
        batch_op.drop_column("merged_into_version_id")
        batch_op.drop_column("reconcile_manifest")
        batch_op.drop_column("block_map")
        batch_op.drop_column("workspace_id")
        batch_op.drop_column("nav_description")
    unique_key_prepass.retract_parent_unique_key(
        "context_page", "uq_context_page_workspace_id"
    )


def prepare_downgrade_data() -> None:
    """Restore the 0001 provenance array before the final page-table batch."""

    connection = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(connection).get_columns("context_page")
    }
    # Asserted rather than skipped, for the same reason as
    # tenancy_schema.prepare_downgrade_data_columns: this runs once, the
    # upgrade dropped the column, and a skip would send
    # _restore_page_provenance at a column whose state nobody predicted.
    assert "created_from_ask_turn_ids" not in columns, (
        "context_page.created_from_ask_turn_ids already exists at prepare "
        "time; the downgrade order changed and this step would have silently "
        "skipped restoring it"
    )
    op.add_column(
        "context_page",
        sa.Column("created_from_ask_turn_ids", sa.JSON(), nullable=True),
    )
    _restore_page_provenance(connection)


def _add_context_columns() -> None:
    op.add_column(
        "context_page",
        sa.Column(
            "stale_candidate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "context_page",
        sa.Column("public_exposure_scope", sa.Text(), nullable=True),
    )
    op.add_column(
        "context_page",
        sa.Column("public_exposed_version_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "context_page_version",
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "context_page_version",
        sa.Column("nav_description", sa.Text(), nullable=True),
    )
    op.add_column(
        "context_page_version",
        sa.Column("block_map", sa.JSON(none_as_null=True), nullable=True),
    )
    op.add_column(
        "context_page_version",
        sa.Column("reconcile_manifest", sa.JSON(none_as_null=True), nullable=True),
    )
    op.add_column(
        "context_page_version",
        sa.Column("merged_into_version_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "context_page_version",
        sa.Column("editing_revision", sa.Integer(), nullable=False, server_default="1"),
    )


def _backfill_version_workspaces(connection: sa.engine.Connection) -> None:
    connection.execute(
        sa.text(
            "UPDATE context_page_version SET workspace_id = ("
            "SELECT workspace_id FROM context_page "
            "WHERE context_page.id = context_page_version.page_id)"
        )
    )
    unresolved = connection.scalar(
        sa.text("SELECT COUNT(*) FROM context_page_version WHERE workspace_id IS NULL")
    )
    if unresolved:
        raise RuntimeError(
            "context_page_version.workspace_id backfill left "
            f"{unresolved} unresolved row(s)"
        )


def _publish_owner_unique_keys() -> None:
    """Expose both parent keys before either table of the pair is rebuilt."""

    for table_name, constraint, columns, temp_index in _PARENT_UNIQUE_KEYS:
        unique_key_prepass.publish_parent_unique_key(
            table_name, constraint, list(columns), temp_index
        )


def _retire_owner_unique_keys() -> None:
    """Remove the SQLite scaffolding. A no-op where the keys are durable."""

    for table_name, _constraint, _columns, temp_index in _PARENT_UNIQUE_KEYS:
        unique_key_prepass.retire_parent_unique_key(table_name, temp_index)


def _restore_page_provenance(connection: sa.engine.Connection) -> None:
    pages = sa.table(
        "context_page",
        sa.column("id", sa.Uuid()),
        sa.column("created_from_ask_turn_ids", sa.JSON()),
    )
    provenance = sa.table(
        "page_provenance",
        sa.column("page_id", sa.Uuid()),
        sa.column("source_kind", sa.Text()),
        sa.column("source_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    by_page: dict[UUID, list[str]] = {}
    for page_id, source_id in connection.execute(
        sa.select(provenance.c.page_id, provenance.c.source_id)
        .where(provenance.c.source_kind == "ask_turn")
        .order_by(provenance.c.created_at, provenance.c.source_id)
    ):
        by_page.setdefault(page_id, []).append(str(source_id))
    for page_id, turn_ids in by_page.items():
        connection.execute(
            pages.update()
            .where(pages.c.id == page_id)
            .values(created_from_ask_turn_ids=turn_ids)
        )
    connection.execute(
        pages.update()
        .where(pages.c.created_from_ask_turn_ids.is_(None))
        .values(created_from_ask_turn_ids=[])
    )


def _apply_public_exposure_grants(connection: sa.engine.Connection) -> None:
    """Turn valid legacy ``is_public`` flags into durable grants, revoke the rest.

    A legacy flag is honoured only when the page sits in a public Workspace and
    points at its own current published snapshot. Archived, headless,
    cross-page and private-Workspace flags are revoked rather than guessed.

    Two statements, in this order: the first grants, the second revokes every
    row the first did not reach. Both must run after ``_add_context_columns``
    (they write the two new grant columns) and before the ``context_page``
    batch installs ``context_page_public_exposure_complete_check``, which
    rejects a row that is public with either grant column NULL.
    """

    context_page = sa.table(
        "context_page",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("lifecycle_status", sa.Text()),
        sa.column("current_published_version_id", sa.Uuid()),
        sa.column("is_public", sa.Boolean()),
        sa.column("public_exposure_scope", sa.Text()),
        sa.column("public_exposed_version_id", sa.Uuid()),
    )
    context_page_version = sa.table(
        "context_page_version",
        sa.column("id", sa.Uuid()),
        sa.column("page_id", sa.Uuid()),
        sa.column("version_status", sa.Text()),
    )
    workspace = sa.table(
        "workspace",
        sa.column("id", sa.Uuid()),
        sa.column("visibility", sa.Text()),
    )

    valid_legacy_grant = sa.and_(
        context_page.c.is_public.is_(True),
        context_page.c.lifecycle_status != "archived",
        context_page.c.current_published_version_id.is_not(None),
        sa.exists(
            sa.select(sa.literal(1)).where(
                workspace.c.id == context_page.c.workspace_id,
                workspace.c.visibility == "public",
            )
        ),
        sa.exists(
            sa.select(sa.literal(1)).where(
                context_page_version.c.id
                == context_page.c.current_published_version_id,
                context_page_version.c.page_id == context_page.c.id,
                context_page_version.c.version_status == "published_snapshot",
            )
        ),
    )

    connection.execute(
        sa.update(context_page)
        .where(valid_legacy_grant)
        .values(
            public_exposure_scope="indexed_web",
            public_exposed_version_id=context_page.c.current_published_version_id,
        )
    )
    connection.execute(
        sa.update(context_page)
        .where(
            context_page.c.is_public.is_(True),
            context_page.c.public_exposure_scope.is_(None),
        )
        .values(is_public=False)
    )


def upgrade_context_editing() -> None:
    """Install the editing-only partial index as its own phase.

    Kept separate for orchestrator readability, not for a technical reason.
    An earlier version of this docstring claimed alembic's batch reflection
    drops a partial predicate across a table rebuild; it does not, on the
    alembic this repo pins. ``uq_cpv_current_published_per_page``
    (``WHERE version_status = 'published_snapshot'``) and
    ``workspace_tree_node_folder_slug_uniq`` both survive their own table's
    rebuild with the predicate intact. Folding this index into the
    ``context_page_version`` batch above is therefore a safe simplification
    whenever the two extra orchestrator entry points become a nuisance.
    """

    op.create_index(
        "uq_cpv_current_editing_per_author",
        "context_page_version",
        ["page_id", "created_by_actor_kind", "created_by_actor_id"],
        unique=True,
        sqlite_where=sa.text("version_status = 'editing'"),
        postgresql_where=sa.text("version_status = 'editing'"),
    )


def downgrade_context_editing() -> None:
    _refuse_downgrade_with_existing_editing_rows(op.get_bind())
    op.drop_index(
        "uq_cpv_current_editing_per_author",
        table_name="context_page_version",
    )


def _refuse_downgrade_with_existing_editing_rows(
    connection: sa.engine.Connection,
) -> None:
    """Fail loud rather than corrupt or silently drop live editing sessions.

    Restoring the old 3-value CHECK would reject every `editing` row outright
    (PostgreSQL) or drop them during SQLite's batch table copy. Neither is a
    safe automatic conversion -- an open editing session is live, unsubmitted
    author work, not disposable derived state. There is no production data
    for this branch to migrate (P0/PLAN section 10), so this phase does not
    attempt one; an operator downgrading past this point must resolve
    (submit, discard, or otherwise account for) any open editing session
    first, the same "deliberately irreversible" posture already used by
    workspace_schema.py's Context History normalization.
    """

    context_page_version = sa.table(
        "context_page_version",
        sa.column("version_status", sa.Text()),
    )
    remaining = connection.execute(
        sa.select(sa.func.count())
        .select_from(context_page_version)
        .where(context_page_version.c.version_status.in_(("editing", "discarded")))
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"Cannot downgrade: {remaining} context_page_version row(s) are "
            "'editing' or 'discarded'. Submit/discard any open editing "
            "session, and account for discarded rows, before downgrading "
            "past the 0002 Context schema finalization."
        )
