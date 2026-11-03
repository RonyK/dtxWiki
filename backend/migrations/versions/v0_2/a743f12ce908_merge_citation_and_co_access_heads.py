"""Merge independent citation and cumulative co-access schema branches.

Revision ID: a743f12ce908
Revises: c58f1d2e890a, 7c2bde1490af

Preserve both original migrations and join their heads without data operations.
"""

revision = "a743f12ce908"
down_revision = ("c58f1d2e890a", "7c2bde1490af")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
