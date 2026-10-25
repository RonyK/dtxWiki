"""Index review decision history and latest-decision lookups.

Revision ID: 333a67e22cd7
Revises: ef53a0a7061c
Create Date: 2026-10-24 23:51:38.941386

Why this index exists
---------------------
Review detail reads its decision history in this order; undo reads the same
history with LIMIT 2 to find the latest decision and recognize an undo retry::

    SELECT ... FROM review_decision
    WHERE review_item_id = :review_id
    ORDER BY created_at DESC, id DESC
    -- Undo also filters workspace_id and limits the result.

The primary key on id and the audience_user_id index do not support this
review-specific lookup. The existing composite foreign key enforces ownership
but does not automatically create a child-table index on SQLite or PostgreSQL.

The leading review_item_id narrows the scan to one review; created_at DESC
matches the history order, and id DESC gives deterministic ordering when
timestamps tie. The same index supports full history and bounded latest-row
reads, so an additional single-column review_item_id index is unnecessary.
ReviewItem.id is already globally unique. Putting workspace_id first would
not serve the detail query above, which filters only review_item_id. Existing
workspace authorization and foreign-key constraints remain responsible for
isolation; this index does not replace either.

Review detail separately reuses its already-loaded history for the undo
projection. That removes a redundant SELECT; this migration improves the
remaining reads. The optimizer still chooses a plan based on table statistics,
so the index does not promise a measured speedup for every table size.

Deployment and rollback
-----------------------
This is a new revision after the existing head, not an edit to a released
migration. Keep the ORM index declaration in backend/models/review.py aligned
so metadata-created databases and upgraded installations use the same shape.

Only a non-unique index is added: no decision rows, defaults, constraints, or
audit payloads are changed, and no data backfill or table rebuild is needed.
Building the index still scans existing rows and needs disk space and build
time; future decision writes also maintain it. Use ordinary Alembic DDL on both
supported dialects. PostgreSQL CREATE INDEX blocks table writes while building;
SQLite requires its normal schema/write locks. This is not a concurrent or
lock-free operation and should run through the normal database upgrade flow.

Downgrade drops only this index. All decision and undo history is preserved;
queries remain correct, with the previous lookup cost. Re-upgrade recreates it.
No IF NOT EXISTS is used: unexpected pre-existing schema should surface rather
than be silently accepted without checking the index definition.
"""

import sqlalchemy as sa
from alembic import op

revision = "333a67e22cd7"
down_revision = "ef53a0a7061c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revision-owned SQL expressions keep historical replays independent of
    # future ORM changes. DESC applies to both timestamp and its tie-breaker.
    op.create_index(
        "ix_review_decision_review_created_id",
        "review_decision",
        ["review_item_id", sa.column("created_at").desc(), sa.column("id").desc()],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_review_decision_review_created_id", table_name="review_decision")
