"""Final v0.2 Inbox tables and retained deleted-recipient fences."""

import sqlalchemy as sa
from alembic import op

_MODE_CHECK = "mode IN ('stored', 'mark')"
_MODE_COLUMNS_CHECK = (
    "(mode = 'stored' AND revision IS NOT NULL AND event_at IS NOT NULL)"
    " OR (mode = 'mark' AND revision IS NULL AND ref IS NULL"
    " AND event_at IS NULL)"
)


def _entry_columns() -> list[sa.Column]:
    return [
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("item_key", sa.Text(), primary_key=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=True),
        sa.Column("ref", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_revision", sa.Text(), nullable=True),
        sa.Column("archived_revision", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "workspace_inbox_entry",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        *_entry_columns(),
        sa.CheckConstraint(_MODE_CHECK, name="workspace_inbox_entry_mode_check"),
        sa.CheckConstraint(
            _MODE_COLUMNS_CHECK, name="workspace_inbox_entry_mode_columns_check"
        ),
    )
    op.create_index(
        "ix_workspace_inbox_entry_list",
        "workspace_inbox_entry",
        ["workspace_id", "user_id", "mode", "event_at", "kind", "item_key"],
    )
    op.create_table(
        "account_inbox_entry",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        *_entry_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(_MODE_CHECK, name="account_inbox_entry_mode_check"),
        sa.CheckConstraint(
            _MODE_COLUMNS_CHECK, name="account_inbox_entry_mode_columns_check"
        ),
    )
    op.create_index(
        "ix_account_inbox_entry_list",
        "account_inbox_entry",
        ["user_id", "mode", "event_at", "kind", "item_key"],
    )

    _create_recipients()


def downgrade() -> None:
    op.drop_table("workspace_inbox_recipient")
    op.drop_index("ix_account_inbox_entry_list", table_name="account_inbox_entry")
    op.drop_table("account_inbox_entry")
    op.drop_index("ix_workspace_inbox_entry_list", table_name="workspace_inbox_entry")
    op.drop_table("workspace_inbox_entry")


def _create_recipients() -> None:
    recipients = op.create_table(
        "workspace_inbox_recipient",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    users = sa.table("users", sa.column("id", sa.Uuid()))
    changes = sa.table(
        "change_set",
        sa.column("workspace_id", sa.Uuid()),
        sa.column("actor_type", sa.Text()),
        sa.column("actor_id", sa.Uuid()),
    )
    entries = sa.table(
        "workspace_inbox_entry",
        sa.column("workspace_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
    )

    def missing_user(user_id):
        return ~sa.select(users.c.id).where(users.c.id == user_id).exists()

    deleted_recipients = sa.union(
        sa.select(changes.c.workspace_id, changes.c.actor_id.label("user_id")).where(
            changes.c.actor_type == "human",
            changes.c.actor_id.is_not(None),
            missing_user(changes.c.actor_id),
        ),
        sa.select(entries.c.workspace_id, entries.c.user_id).where(
            missing_user(entries.c.user_id)
        ),
    ).subquery()
    op.execute(
        recipients.insert().from_select(
            ["workspace_id", "user_id", "deleted_at"],
            sa.select(
                deleted_recipients.c.workspace_id,
                deleted_recipients.c.user_id,
                sa.func.current_timestamp(),
            ),
        )
    )
    op.execute(entries.delete().where(missing_user(entries.c.user_id)))
