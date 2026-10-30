"""Fence workspace Inbox recipients after final account deletion.

Revision ID: 6f4a9f50c046
Revises: af62c98d0731
Create Date: 2026-10-29 23:03:10.493488

The recipient key stays on the workspace plane without identity foreign keys.
``deleted_at`` is NULL for a live recipient and records a final deletion for
an account whose retained shared history must never recreate personal entries.

This single-database upgrade also fences accounts purged before the recipient
table existed. Missing human authors in retained ChangeSets and missing users
named by workspace Inbox entries supply the known recipient keys. Tombstones
are inserted before those orphan Inbox rows are deleted; shared history and
every current user's personal state stay intact. Live recipients are created
lazily by their first write rather than backfilled for every workspace member.

Downgrade drops the recipient table and its fences. It cannot restore the
orphan personal entries reclaimed by upgrade. Re-upgrade derives fences again
from the retained history and any remaining orphan entries.
"""

import sqlalchemy as sa
from alembic import op

revision = "6f4a9f50c046"
down_revision = "af62c98d0731"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("workspace_inbox_recipient")
