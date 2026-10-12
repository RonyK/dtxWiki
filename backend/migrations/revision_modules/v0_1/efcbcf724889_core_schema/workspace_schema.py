"""Add account recovery, Workspace membership, and Context safeguards."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection


def upgrade() -> None:
    bind = op.get_bind()

    # A published head whose `latest_draft_version_id` pointer is unset can
    # carry `draft`/`linear` ancestor rows stranded by the pre-fix
    # `apply_context_publication` (fixed going forward in
    # context_lifecycle.py; historical rows on any database that already ran
    # the old code need the same demotion here, and may have accumulated it
    # across more than one publish). Run before the preflight below, so this
    # common shape becomes a silent no-op instead of a hard upgrade refusal.
    # A rejected-and-not-yet-restarted review draft is the same pointer
    # shape but is NOT dead history -- both this function and the preflight
    # count below carve it out separately (see
    # `_rejected_restartable_version_ids`).
    _demote_pre_publish_orphan_drafts(bind)

    # Run the only blocking data preflight before any SQLite batch DDL. If an
    # operator must repair ambiguous Context pointers, the outer 0002
    # savepoint returns the database to the 0001 boundary without a partially
    # applied Workspace schema.
    _assert_context_history_preflight(bind)

    # --- 1. Account recovery foundation ---------------------------------
    # Existing emails intentionally remain unverified. The column is folded
    # into the final users-table batch below.
    op.create_table(
        "password_reset_tokens",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("delivery", sa.Text(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("requested_via", sa.Text(), nullable=False),
        sa.Column(
            "revoke_api_tokens",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('email_verification', 'password_reset')",
            name="password_reset_token_purpose_check",
        ),
        sa.CheckConstraint(
            "delivery IN ('email', 'link')",
            name="password_reset_token_delivery_check",
        ),
        sa.CheckConstraint(
            "requested_via IN ('public', 'server_admin', 'cli')",
            name="password_reset_token_requested_via_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_digest",
            name="uq_password_reset_token_digest",
        ),
    )
    op.create_index(
        "idx_password_reset_token_user_purpose",
        "password_reset_tokens",
        ["user_id", "purpose", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_password_reset_token_active_user_purpose",
        "password_reset_tokens",
        ["user_id", "purpose"],
        unique=True,
        sqlite_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )

    # --- 2. Workspace MVP Personal foundation ---------------------------
    op.execute(
        sa.text(
            "UPDATE workspace SET visibility = 'private' "
            "WHERE visibility NOT IN ('private', 'team', 'public')"
        )
    )
    # Additive columns do not require a SQLite table copy. Constraints and
    # directory_title_key's post-backfill NOT NULL transition are combined in
    # the one final workspace batch below.
    op.add_column(
        "workspace",
        sa.Column(
            "join_policy",
            sa.Text(),
            nullable=False,
            server_default="invite_only",
        ),
    )
    op.add_column(
        "workspace",
        sa.Column("directory_title_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "workspace",
        sa.Column(
            "edge_projection_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "workspace",
        sa.Column(
            "authority_projection_version",
            sa.Integer(),
            nullable=False,
            server_default="-1",
        ),
    )
    op.add_column(
        "workspace",
        sa.Column(
            "ask_retrieval_mode",
            sa.Text(),
            nullable=False,
            server_default="auto",
        ),
    )
    with op.batch_alter_table("workspace_memberships") as batch_op:
        batch_op.create_check_constraint(
            "workspace_membership_role_check",
            "role IN ('viewer', 'editor', 'owner')",
        )
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("personal_workspace_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("member_search_key", sa.Text(), nullable=True),
    )
    _clear_nonpublic_workspace_page_flags(bind)
    _backfill_personal_workspaces(bind)

    # Persist the exact NFKC/casefold/whitespace normalization used for
    # directory and member search. SQL lower()/trim() are not equivalent for
    # Unicode and would change ordering/cursor semantics across dialects.
    _backfill_directory_search_keys(bind)
    with op.batch_alter_table("workspace") as batch_op:
        batch_op.alter_column(
            "directory_title_key",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "workspace_join_policy_check",
            "join_policy IN ('invite_only', 'request')",
        )
        batch_op.create_check_constraint(
            "workspace_private_join_policy_check",
            "visibility != 'private' OR join_policy = 'invite_only'",
        )
        batch_op.create_check_constraint(
            "workspace_visibility_check",
            "visibility IN ('private', 'team', 'public')",
        )
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "member_search_key",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_users_personal_workspace_id_workspace",
            "workspace",
            ["personal_workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_users_personal_workspace_id",
            ["personal_workspace_id"],
        )
    directory_collation = "C" if bind.dialect.name == "postgresql" else "BINARY"
    op.create_index(
        "ix_workspace_directory_title_key",
        "workspace",
        [
            sa.column("directory_title_key").collate(directory_collation),
            "id",
        ],
        unique=False,
    )
    op.create_index(
        "ix_users_member_search_key",
        "users",
        ["member_search_key"],
        unique=False,
    )

    # --- 3. Recovery mail outbox and shared rate limits -----------------
    op.create_table(
        "email_outbox",
        sa.Column("recipient_user_id", sa.Uuid(), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_email", sa.Text(), nullable=False),
        sa.Column("message_kind", sa.Text(), nullable=False),
        sa.Column("source_job_type", sa.Text(), nullable=True),
        sa.Column("encrypted_token", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "message_kind IN "
            "('password_reset', 'email_verification', 'password_changed', "
            "'job_failed', 'reconciliation_reminder')",
            name="email_outbox_message_kind_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed')",
            name="email_outbox_status_check",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["password_reset_tokens.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_email_outbox_claim",
        "email_outbox",
        ["status", "next_attempt_at", "created_at"],
        unique=False,
    )
    op.create_table(
        "password_recovery_rate_limit_slots",
        sa.Column("bucket_kind", sa.Text(), nullable=False),
        sa.Column("bucket_digest", sa.Text(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "bucket_kind IN ('ip', 'email', 'global')",
            name="password_recovery_rate_limit_bucket_kind_check",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bucket_digest",
            "window_started_at",
            "slot",
            name="uq_password_recovery_rate_limit_slot",
        ),
    )
    op.create_index(
        "idx_password_recovery_rate_limit_expiry",
        "password_recovery_rate_limit_slots",
        ["expires_at"],
        unique=False,
    )

    # --- 4. Workspace discovery join-request lifecycle ------------------
    op.create_table(
        "workspace_join_requests",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("requester_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled', 'closed')",
            name="workspace_join_request_status_check",
        ),
        sa.CheckConstraint(
            "attempt_no >= 1",
            name="workspace_join_request_attempt_check",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["requester_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "requester_user_id",
            name="workspace_join_request_user_workspace_uq",
        ),
    )
    op.create_index(
        "idx_workspace_join_request_queue",
        "workspace_join_requests",
        ["workspace_id", "status", "requested_at", "id"],
        unique=False,
    )
    op.create_index(
        "idx_workspace_join_request_requester",
        "workspace_join_requests",
        ["requester_user_id", "status"],
        unique=False,
    )

    # --- 5. Context History published-lineage normalization -------------
    _normalize_context_history(bind)
    op.create_index(
        "uq_cpv_current_published_per_page",
        "context_page_version",
        ["page_id"],
        unique=True,
        sqlite_where=sa.text("version_status = 'published_snapshot'"),
        postgresql_where=sa.text("version_status = 'published_snapshot'"),
    )

    # --- 6. Review decision_summary machine code (I18N-08a) --------------
    # `decision_summary` holds English prose written straight into the row
    # (context_write_service.py), so it can never be localized and is baked
    # into every historical review permanently. This column carries a stable
    # machine code for the fixed-copy cases so the frontend can localize
    # from the code and fall back to the stored prose (existing rows, and
    # any caller-supplied free text such as `risk_notes`) when it is unset.
    # `decision_summary` itself is kept for backward compatibility; nothing
    # here touches existing rows.
    # Plain add_column, not a batch: alembic's recreate="auto" never rebuilds
    # a SQLite table just to add one. The batch form used to suggest
    # review_item spends its one allowed rebuild here, which is the opposite
    # of what the downgrade note below spends nine lines establishing.
    op.add_column(
        "review_item", sa.Column("decision_summary_code", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    # --- 6'. Review decision_summary machine code (I18N-08a) -------------
    # upgrade() adds review_item.decision_summary_code here, but the DROP lives
    # in tenancy_schema._revert_final_table_constraints, which already opens a
    # batch for review_item to remove that table's unique constraint and
    # foreign key. Dropping it here instead would be a second SQLite table
    # rebuild of review_item and fail
    # test_epic1_rebuilds_each_sqlite_table_at_most_once_per_direction. There
    # used to be a drop in BOTH places, the one here behind a column-existence
    # probe that silently no-opped -- which made a missing drop
    # indistinguishable from a correct one. One owner, named on both sides.

    # --- 5'. Context History normalization ------------------------------
    # Deliberately irreversible: restoring multiple rows to a misleading
    # "published" state would reintroduce the inconsistency this repair fixes.
    op.drop_index(
        "uq_cpv_current_published_per_page",
        table_name="context_page_version",
    )

    # --- 4'. Workspace discovery join-request lifecycle -----------------
    op.drop_index(
        "idx_workspace_join_request_requester",
        table_name="workspace_join_requests",
    )
    op.drop_index(
        "idx_workspace_join_request_queue",
        table_name="workspace_join_requests",
    )
    op.drop_table("workspace_join_requests")

    # --- 3'. Recovery mail outbox and shared rate limits ----------------
    op.drop_index(
        "idx_password_recovery_rate_limit_expiry",
        table_name="password_recovery_rate_limit_slots",
    )
    op.drop_table("password_recovery_rate_limit_slots")
    op.drop_index("idx_email_outbox_claim", table_name="email_outbox")
    op.drop_table("email_outbox")

    # --- 2'. Workspace MVP Personal foundation --------------------------
    # Personal workspaces are ordinary user data and may contain content after
    # upgrade, so downgrade removes only the anchor/policy schema. It does not
    # delete those workspaces or their memberships.
    op.drop_index("ix_users_member_search_key", table_name="users")
    op.drop_index(
        "ix_workspace_directory_title_key",
        table_name="workspace",
    )
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("member_search_key")
        batch_op.drop_constraint(
            "uq_users_personal_workspace_id",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_users_personal_workspace_id_workspace",
            type_="foreignkey",
        )
        batch_op.drop_column("personal_workspace_id")
        batch_op.drop_column("email_verified_at")
    with op.batch_alter_table("workspace_memberships") as batch_op:
        batch_op.drop_constraint(
            "workspace_membership_role_check",
            type_="check",
        )
    with op.batch_alter_table("workspace") as batch_op:
        batch_op.drop_constraint(
            "workspace_visibility_check",
            type_="check",
        )
        batch_op.drop_constraint(
            "workspace_private_join_policy_check",
            type_="check",
        )
        batch_op.drop_constraint(
            "workspace_join_policy_check",
            type_="check",
        )
        batch_op.drop_column("ask_retrieval_mode")
        batch_op.drop_column("authority_projection_version")
        batch_op.drop_column("edge_projection_version")
        batch_op.drop_column("directory_title_key")
        batch_op.drop_column("join_policy")

    # --- 1'. Account recovery foundation --------------------------------
    op.drop_index(
        "uq_password_reset_token_active_user_purpose",
        table_name="password_reset_tokens",
    )
    op.drop_index(
        "idx_password_reset_token_user_purpose",
        table_name="password_reset_tokens",
    )
    op.drop_table("password_reset_tokens")


_PERSONAL_WORKSPACE_NAMESPACE = UUID("77e369b1-cf27-47ca-82f8-68b2adfa1634")


def _rejected_restartable_version_ids(
    connection: Connection,
    candidates: list[tuple[UUID, UUID]],
) -> set[UUID]:
    """Which ``(version_id, page_id)`` candidates a REJECTED review still
    names, i.e. deliberately-preserved restart material rather than dead
    history.

    Mirrors ``ContextWriteService._is_restartable_rejected_version``
    (backend/services/context_write_service.py) exactly: a rejected
    ``context_update`` review's ``ChangeOperation.diff_ref`` still names the
    version it rejected (``"context_page_version:<id>"``), and
    ``update_draft``'s ``restart_after_reject`` path forks a fresh draft
    directly from that row later. Both discard-like predicates below
    (``_demote_pre_publish_orphan_drafts`` and the
    ``latest_draft_unset_with_live_draft`` preflight count) must treat that
    row as live, not orphaned -- a reachability-walk fix does not help here
    either, the same way it does not help the sibling function above: the
    row is unreachable from any pointer by design until a human restarts it.

    Matching is done in Python, not a raw-SQL string join, because
    ``diff_ref`` embeds ``str(version.id)`` (the canonical hyphenated
    form -- every write path builds it as an f-string against the ORM
    ``UUID`` object) while the ``context_page_version.id`` column's on-disk
    representation is dialect-dependent (SQLite's ``Uuid`` type stores
    32-char hex with no dashes; PostgreSQL uses its native ``uuid`` type).
    Decoding through ``.columns(..., sa.Uuid(as_uuid=True))`` normalizes both
    sides back to real ``uuid.UUID`` objects so the comparison is exact on
    every dialect, instead of relying on fragile ad hoc dialect-specific
    text formatting.
    """

    if not candidates:
        return set()
    diff_ref_by_id = {
        version_id: f"context_page_version:{version_id}"
        for version_id, _page_id in candidates
    }
    rows = connection.execute(
        sa.text(
            "SELECT DISTINCT change_operation.diff_ref AS diff_ref, "
            "change_operation.target_id AS target_id "
            "FROM review_item "
            "JOIN change_set ON change_set.id = review_item.change_set_id "
            "JOIN change_operation "
            "ON change_operation.change_set_id = change_set.id "
            "WHERE review_item.review_type = 'context_update' "
            "AND review_item.status = 'rejected' "
            "AND change_operation.target_type = 'context_page' "
            "AND change_operation.diff_ref IN :refs"
        )
        .bindparams(sa.bindparam("refs", expanding=True))
        .columns(diff_ref=sa.Text(), target_id=sa.Uuid(as_uuid=True)),
        {"refs": list(diff_ref_by_id.values())},
    ).all()
    rejected_pairs = {(row.diff_ref, row.target_id) for row in rows}
    return {
        version_id
        for version_id, page_id in candidates
        if (diff_ref_by_id[version_id], page_id) in rejected_pairs
    }


def _demote_pre_publish_orphan_drafts(connection: Connection) -> None:
    """Demote draft/linear rows a page's publish history could only have
    stranded, using ``version_no`` rather than a parent-pointer walk.

    A parent-pointer walk (mirroring the write-side fix in
    ``apply_context_publication``, backend/services/context_lifecycle.py)
    is precise but only reaches the *most recent* publish's orphan: after a
    SECOND publish, the first publish's demoted head sits between the new
    head and the older orphan as a ``superseded_snapshot`` row, and a walk
    that requires every intermediate row to still be ``draft``/``linear``
    stops there without ever reaching it. Real installations accumulate
    multiple publishes, so that walk alone under-cleans history here (it
    remains the correct rule for the write side, which is only ever
    cleaning up the ONE chain it just published).

    ``version_no`` is `` MAX(version_no) + 1`` per page at row-insert time
    (backend/repositories/context_page_repo.py) and is guarded unique per
    page (``context_page_version_no_uq``), so it is strictly monotonic: a
    row's `version_no` records exactly how many versions existed on the page
    before it, regardless of how many times the page has been published
    since. On a page whose ``latest_draft_version_id`` is unset, no draft is
    addressable through any pointer at all, so a draft older than the
    published head (``version_no`` less than the head's) is USUALLY provably
    dead history rather than a live draft a human might still want.

    The one exception is a REJECTED, not-yet-restarted draft: rejecting a
    review deliberately clears ``latest_draft_version_id`` while leaving the
    rejected version ``draft``/``linear`` (``review_service.py``'s
    ``decide_review``, so ``update_draft``'s ``restart_after_reject`` path
    can fork a fresh draft from it later), which is exactly this row shape.
    If any unrelated later edit on the same page gets published before that
    restart happens, the rejected draft's ``version_no`` falls behind the
    new head and looks identical to true dead history to the rule above.
    Demoting it anyway would flip it to ``graph_status='discarded'``; restart
    would still succeed (``_is_restartable_rejected_version`` only checks
    ``version_status``), but publishing the restarted draft would then fail
    with ``StaleContextDraftError`` the moment
    ``assert_context_publish_candidate`` walks back over the discarded
    ancestor and finds it no longer ``linear`` -- silently and permanently
    breaking the documented reject-then-restart recovery path. Excluding
    rows still named by a rejected review keeps the "provably dead" claim
    true: a draft is only ever discarded here when nothing -- not even a
    resolved review -- still needs it.

    A draft *newer* than the head is left untouched deliberately: that is
    genuine ambiguity (which of possibly several is the intended edit head?)
    that the preflight below must still catch and block. A page that has
    never published (``current_published_version_id IS NULL``) is a
    different, already correctly handled violation class and is left alone.
    """

    candidates = connection.execute(
        sa.text(
            "SELECT context_page_version.id AS id, "
            "context_page_version.page_id AS page_id "
            "FROM context_page_version "
            "WHERE version_status = 'draft' AND graph_status = 'linear' "
            "AND EXISTS ("
            "SELECT 1 FROM context_page AS page "
            "JOIN context_page_version AS head "
            "ON head.id = page.current_published_version_id "
            "WHERE page.id = context_page_version.page_id "
            "AND page.latest_draft_version_id IS NULL "
            "AND context_page_version.version_no < head.version_no"
            ")"
        ).columns(id=sa.Uuid(as_uuid=True), page_id=sa.Uuid(as_uuid=True))
    ).all()
    if not candidates:
        return

    pairs = [(row.id, row.page_id) for row in candidates]
    preserved = _rejected_restartable_version_ids(connection, pairs)
    demote_ids = [
        version_id for version_id, _page_id in pairs if version_id not in preserved
    ]
    if not demote_ids:
        return

    connection.execute(
        sa.text(
            "UPDATE context_page_version SET graph_status = 'discarded' "
            "WHERE id IN :ids"
        ).bindparams(sa.bindparam("ids", expanding=True, type_=sa.Uuid(as_uuid=True))),
        {"ids": demote_ids},
    )


def _count_pages_with_unset_latest_draft_and_live_draft(connection: Connection) -> int:
    """Pages the ``latest_draft_unset_with_live_draft`` preflight must block.

    Same shape as the plain ``COUNT`` this replaces -- a page whose
    ``latest_draft_version_id`` is unset while a ``draft``/``linear`` row
    still sits on it -- with one exclusion: a row a rejected
    ``context_update`` review still names (see
    ``_rejected_restartable_version_ids`` above) is deliberately-preserved
    restart material, not the "reachable from nothing" ambiguity this count
    exists to catch. Without this exclusion, ANY database holding even one
    rejected-and-not-yet-restarted review -- ordinary, frequent activity,
    independent of ``version_no`` or a second publish -- would fail
    ``alembic upgrade head`` with no repair path that does not itself
    silently overturn the rejection (``repair_context_history`` would
    restore the rejected draft as the live edit head; see
    ``_proven_linear_draft``, backend/services/context_lifecycle.py).

    A page still blocks if it holds any OTHER unset linear draft alongside a
    rejected one (genuine ambiguity is untouched), or if a rejected draft's
    own review has since been superseded/re-decided in a way that no longer
    reads as ``rejected`` (nothing here treats that row as special then).
    """

    candidates = connection.execute(
        sa.text(
            "SELECT context_page_version.id AS id, "
            "context_page_version.page_id AS page_id "
            "FROM context_page_version "
            "JOIN context_page AS page ON page.id = context_page_version.page_id "
            "WHERE context_page_version.version_status = 'draft' "
            "AND context_page_version.graph_status = 'linear' "
            "AND page.latest_draft_version_id IS NULL"
        ).columns(id=sa.Uuid(as_uuid=True), page_id=sa.Uuid(as_uuid=True))
    ).all()
    if not candidates:
        return 0

    pairs = [(row.id, row.page_id) for row in candidates]
    preserved = _rejected_restartable_version_ids(connection, pairs)
    blocking_page_ids = {
        page_id for version_id, page_id in pairs if version_id not in preserved
    }
    return len(blocking_page_ids)


def _context_history_preflight_counts(connection: Connection) -> dict[str, int]:
    """Return ambiguity counts that must be zero before automatic repair."""

    cross_page_or_missing_head = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM context_page AS page "
            "LEFT JOIN context_page_version AS head "
            "ON head.id = page.current_published_version_id "
            "WHERE page.current_published_version_id IS NOT NULL "
            "AND (head.id IS NULL OR head.page_id <> page.id)"
        )
    ).scalar_one()
    published_without_head = connection.execute(
        sa.text(
            "SELECT COUNT(DISTINCT version.page_id) "
            "FROM context_page_version AS version "
            "JOIN context_page AS page ON page.id = version.page_id "
            "WHERE version.version_status = 'published_snapshot' "
            "AND page.current_published_version_id IS NULL"
        )
    ).scalar_one()
    current_version_missing_or_cross_page = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM context_page AS page "
            "LEFT JOIN context_page_version AS current_version "
            "ON current_version.id = page.current_version_id "
            "WHERE page.current_version_id IS NOT NULL "
            "AND (current_version.id IS NULL OR current_version.page_id <> page.id)"
        )
    ).scalar_one()
    current_version_status_contradiction = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM context_page AS page "
            "JOIN context_page_version AS current_version "
            "ON current_version.id = page.current_version_id "
            "WHERE current_version.page_id = page.id "
            "AND (page.current_published_version_id IS NULL "
            "OR current_version.id <> page.current_published_version_id) "
            "AND (current_version.version_status <> 'draft' "
            "OR current_version.graph_status <> 'linear')"
        )
    ).scalar_one()
    latest_draft_missing_or_cross_page = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM context_page AS page "
            "LEFT JOIN context_page_version AS latest_draft "
            "ON latest_draft.id = page.latest_draft_version_id "
            "WHERE page.latest_draft_version_id IS NOT NULL "
            "AND (latest_draft.id IS NULL OR latest_draft.page_id <> page.id)"
        )
    ).scalar_one()
    latest_draft_status_contradiction = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM context_page AS page "
            "JOIN context_page_version AS latest_draft "
            "ON latest_draft.id = page.latest_draft_version_id "
            "WHERE latest_draft.page_id = page.id "
            "AND (page.current_published_version_id IS NULL "
            "OR latest_draft.id <> page.current_published_version_id) "
            "AND (latest_draft.version_status <> 'draft' "
            "OR latest_draft.graph_status <> 'linear')"
        )
    ).scalar_one()
    # `_normalize_context_history` clears a `latest_draft == published head`
    # overlap unconditionally, which is only deterministic while that overlap
    # hides nothing. When the page still holds a live linear draft, the pointer
    # is the sole reference to it and clearing it would strand the draft, so the
    # upgrade must stop and let the documented repair command decide. A unique
    # draft is repairable there, after which the overlap is gone and this count
    # returns to zero; genuine ambiguity stays blocked in both tools.
    latest_draft_overlap_masks_live_draft = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM context_page AS page "
            "WHERE page.current_published_version_id IS NOT NULL "
            "AND page.latest_draft_version_id = page.current_published_version_id "
            "AND EXISTS ("
            "SELECT 1 FROM context_page_version AS draft "
            "WHERE draft.page_id = page.id "
            "AND draft.version_status = 'draft' "
            "AND draft.graph_status = 'linear'"
            ")"
        )
    ).scalar_one()
    # An unset pointer contradicts nothing, so every predicate above passes it
    # and the upgrade would proceed leaving a staged draft reachable from no
    # pointer at all. This migration never guesses an edit head, so both unset
    # classes are blockers that send the operator through the repair command,
    # which restores a uniquely proven draft and refuses an ambiguous one.
    current_version_unset_with_versions = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM context_page AS page "
            "WHERE page.current_version_id IS NULL "
            "AND EXISTS ("
            "SELECT 1 FROM context_page_version AS version "
            "WHERE version.page_id = page.id"
            ")"
        )
    ).scalar_one()
    latest_draft_unset_with_live_draft = (
        _count_pages_with_unset_latest_draft_and_live_draft(connection)
    )
    return {
        "cross_page_or_missing_head": int(cross_page_or_missing_head),
        "published_without_head": int(published_without_head),
        "latest_draft_overlap_masks_live_draft": int(
            latest_draft_overlap_masks_live_draft
        ),
        "current_version_unset_with_versions": int(current_version_unset_with_versions),
        "latest_draft_unset_with_live_draft": int(latest_draft_unset_with_live_draft),
        "current_version_missing_or_cross_page": int(
            current_version_missing_or_cross_page
        ),
        "current_version_status_contradiction": int(
            current_version_status_contradiction
        ),
        "latest_draft_missing_or_cross_page": int(latest_draft_missing_or_cross_page),
        "latest_draft_status_contradiction": int(latest_draft_status_contradiction),
    }


def _clear_nonpublic_workspace_page_flags(connection: Connection) -> None:
    """Prevent legacy private/team pages from retaining anonymous exposure."""

    context_page = sa.table(
        "context_page",
        sa.column("workspace_id", sa.Uuid()),
        sa.column("is_public", sa.Boolean()),
    )
    workspace = sa.table(
        "workspace",
        sa.column("id", sa.Uuid()),
        sa.column("visibility", sa.Text()),
    )
    connection.execute(
        sa.update(context_page)
        .where(
            context_page.c.is_public.is_(True),
            context_page.c.workspace_id.in_(
                sa.select(workspace.c.id).where(workspace.c.visibility != "public")
            ),
        )
        .values(is_public=False)
    )


def _assert_context_history_preflight(connection: Connection) -> None:
    counts = _context_history_preflight_counts(connection)
    failures = {name: count for name, count in counts.items() if count != 0}
    if failures:
        rendered = ", ".join(f"{name}={count}" for name, count in failures.items())
        raise RuntimeError(
            "context history preflight failed; inspect and repair before upgrade: "
            f"{rendered}"
        )


def _normalize_context_history(connection: Connection) -> None:
    """Repair only published lineage that a same-page canonical pointer proves."""

    connection.execute(
        sa.text(
            "UPDATE context_page_version AS version "
            "SET version_status = 'superseded_snapshot', graph_status = 'linear' "
            "WHERE version.version_status = 'published_snapshot' "
            "AND EXISTS ("
            "SELECT 1 FROM context_page AS page "
            "WHERE page.id = version.page_id "
            "AND page.current_published_version_id IS NOT NULL"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM context_page AS page "
            "WHERE page.id = version.page_id "
            "AND page.current_published_version_id = version.id"
            ")"
        )
    )
    # A legacy page can redundantly name its proven published head as the
    # latest draft. Clearing that exact overlap is deterministic; every other
    # latest-draft status contradiction is rejected by the preflight above.
    connection.execute(
        sa.text(
            "UPDATE context_page SET latest_draft_version_id = NULL "
            "WHERE current_published_version_id IS NOT NULL "
            "AND latest_draft_version_id = current_published_version_id"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE context_page_version AS version "
            "SET version_status = 'published_snapshot', graph_status = 'linear' "
            "WHERE EXISTS ("
            "SELECT 1 FROM context_page AS page "
            "WHERE page.id = version.page_id "
            "AND page.current_published_version_id = version.id"
            ")"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE context_page_version "
            "SET graph_status = 'linear' "
            "WHERE version_status = 'superseded_snapshot' "
            "AND graph_status <> 'linear'"
        )
    )


_PERSONAL_WORKSPACE_TITLE = "Personal Workspace"


def _backfill_personal_workspaces(
    connection: Connection,
    *,
    batch_size: int = 100,
) -> None:
    """Provision a resumable Personal anchor for every existing user."""

    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=connection)
    workspaces = sa.Table("workspace", metadata, autoload_with=connection)
    memberships = sa.Table(
        "workspace_memberships",
        metadata,
        autoload_with=connection,
    )
    tree_nodes = sa.Table(
        "workspace_tree_node",
        metadata,
        autoload_with=connection,
    )
    tree_policies = sa.Table("tree_policy", metadata, autoload_with=connection)

    offset = 0
    while True:
        batch = (
            connection.execute(
                sa.select(
                    users.c.id,
                    users.c.default_workspace_id,
                    users.c.personal_workspace_id,
                )
                .order_by(users.c.id)
                .limit(batch_size)
                .offset(offset)
            )
            .mappings()
            .all()
        )
        if not batch:
            return
        for user_row in batch:
            _ensure_migration_personal_workspace(
                connection,
                user_row=user_row,
                users=users,
                workspaces=workspaces,
                memberships=memberships,
                tree_nodes=tree_nodes,
                tree_policies=tree_policies,
            )
        offset += len(batch)


def _backfill_directory_search_keys(connection: Connection) -> None:
    workspace = sa.table(
        "workspace",
        sa.column("id", sa.Uuid()),
        sa.column("title", sa.Text()),
        sa.column("directory_title_key", sa.Text()),
    )
    users = sa.table(
        "users",
        sa.column("id", sa.Uuid()),
        sa.column("display_name", sa.Text()),
        sa.column("email", sa.Text()),
        sa.column("member_search_key", sa.Text()),
    )

    def normalize(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    for workspace_id, title in connection.execute(
        sa.select(workspace.c.id, workspace.c.title)
    ):
        connection.execute(
            workspace.update()
            .where(workspace.c.id == workspace_id)
            .values(directory_title_key=normalize(title))
        )
    for user_id, display_name, email in connection.execute(
        sa.select(users.c.id, users.c.display_name, users.c.email)
    ):
        connection.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(member_search_key=normalize(f"{display_name} {email}"))
        )


def _ensure_migration_personal_workspace(
    connection: Connection,
    *,
    user_row,
    users: sa.Table,
    workspaces: sa.Table,
    memberships: sa.Table,
    tree_nodes: sa.Table,
    tree_policies: sa.Table,
) -> None:
    user_id = user_row["id"]
    workspace = None
    personal_workspace_id = user_row["personal_workspace_id"]
    if personal_workspace_id is not None:
        workspace = (
            connection.execute(
                sa.select(workspaces).where(workspaces.c.id == personal_workspace_id)
            )
            .mappings()
            .one_or_none()
        )

    attempt = 0
    while workspace is None:
        seed = str(user_id) if attempt == 0 else f"{user_id}:{attempt}"
        candidate_uuid = uuid5(_PERSONAL_WORKSPACE_NAMESPACE, seed)
        candidate_id = _migration_uuid_value(connection, candidate_uuid)
        candidate_slug = f"personal-{candidate_uuid.hex}"
        collision = (
            connection.execute(
                sa.select(workspaces)
                .where(
                    sa.or_(
                        workspaces.c.id == candidate_id,
                        workspaces.c.slug == candidate_slug,
                    )
                )
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if collision is not None:
            if (
                collision["id"] == candidate_id
                and collision["slug"] == candidate_slug
                and collision["owner_user_id"] == user_id
                and collision["title"] == _PERSONAL_WORKSPACE_TITLE
            ):
                workspace = collision
                break
            attempt += 1
            continue
        now = datetime.now(UTC)
        connection.execute(
            workspaces.insert().values(
                id=candidate_id,
                slug=candidate_slug,
                title=_PERSONAL_WORKSPACE_TITLE,
                name=_PERSONAL_WORKSPACE_TITLE,
                description=None,
                goal=(
                    "Build a source-grounded workspace for cited answers, "
                    "reusable notes, and team knowledge."
                ),
                default_language="en",
                library_policy={
                    "allowed_sources": [{"type": "manual_upload"}],
                    "denied_sources": [],
                    "manual_approval_required": True,
                    "max_upload_bytes": 2_000_000,
                },
                write_policy={
                    "draft_only": True,
                    "publish_requires_review": True,
                },
                citation_policy={
                    "strictness": "standard",
                    "accepted_citation_types": [
                        "raw_source",
                        "derived_artifact",
                        "context_page",
                    ],
                },
                agent_injection_policy={
                    "bundle_ttl_default_seconds": 3600,
                    "bundle_ttl_max_seconds": 86400,
                    "category_inclusion_policy": {
                        "agent_guide": "always",
                        "persona": "default_on",
                        "skill": "task_matched",
                        "memory": "task_matched",
                    },
                },
                review_policy=_migration_default_review_policy(),
                lint_policy={},
                ask_retrieval_mode="auto",
                auto_fix_enabled=False,
                edge_projection_version=0,
                authority_projection_version=-1,
                visibility="private",
                join_policy="invite_only",
                created_by_actor_id=user_id,
                owner_actor_id=user_id,
                owner_user_id=user_id,
                updated_at=now,
                created_at=now,
            )
        )
        workspace = {
            "id": candidate_id,
            "slug": candidate_slug,
            "title": _PERSONAL_WORKSPACE_TITLE,
            "owner_user_id": user_id,
        }

    workspace_id = workspace["id"]
    connection.execute(
        workspaces.update()
        .where(workspaces.c.id == workspace_id)
        .values(
            title=_PERSONAL_WORKSPACE_TITLE,
            name=_PERSONAL_WORKSPACE_TITLE,
            visibility="private",
            join_policy="invite_only",
            created_by_actor_id=user_id,
            owner_actor_id=user_id,
            owner_user_id=user_id,
        )
    )
    _ensure_migration_personal_tree(
        connection,
        workspace_id=workspace_id,
        user_id=user_id,
        tree_nodes=tree_nodes,
        tree_policies=tree_policies,
    )
    membership = connection.execute(
        sa.select(memberships.c.role).where(
            memberships.c.user_id == user_id,
            memberships.c.workspace_id == workspace_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        connection.execute(
            memberships.insert().values(
                user_id=user_id,
                workspace_id=workspace_id,
                role="owner",
                joined_at=datetime.now(UTC),
            )
        )
    elif membership != "owner":
        connection.execute(
            memberships.update()
            .where(
                memberships.c.user_id == user_id,
                memberships.c.workspace_id == workspace_id,
            )
            .values(role="owner")
        )

    default_workspace_id = user_row["default_workspace_id"]
    default_is_accessible = False
    if default_workspace_id is not None:
        default_is_accessible = (
            connection.execute(
                sa.select(sa.literal(True))
                .select_from(
                    memberships.join(
                        workspaces,
                        memberships.c.workspace_id == workspaces.c.id,
                    )
                )
                .where(
                    memberships.c.user_id == user_id,
                    memberships.c.workspace_id == default_workspace_id,
                )
            ).scalar_one_or_none()
            is True
        )
    values = {"personal_workspace_id": workspace_id}
    if not default_is_accessible:
        values["default_workspace_id"] = workspace_id
    connection.execute(users.update().where(users.c.id == user_id).values(**values))


def _ensure_migration_personal_tree(
    connection: Connection,
    *,
    workspace_id: UUID,
    user_id: UUID,
    tree_nodes: sa.Table,
    tree_policies: sa.Table,
) -> None:
    for section, title, sort_key in (
        ("context", "Context", "0001"),
        ("library", "Library", "0002"),
    ):
        root_id = connection.execute(
            sa.select(tree_nodes.c.id).where(
                tree_nodes.c.workspace_id == workspace_id,
                tree_nodes.c.section == section,
                tree_nodes.c.parent_id.is_(None),
                tree_nodes.c.slug == section,
            )
        ).scalar_one_or_none()
        if root_id is None:
            root_uuid = uuid5(UUID(str(workspace_id)), f"root:{section}")
            root_id = _migration_uuid_value(connection, root_uuid)
            now = datetime.now(UTC)
            connection.execute(
                tree_nodes.insert().values(
                    id=root_id,
                    workspace_id=workspace_id,
                    section=section,
                    node_kind="folder",
                    parent_id=None,
                    sort_key=sort_key,
                    title=title,
                    slug=section,
                    target_kind=None,
                    target_id=None,
                    is_system_node=True,
                    can_have_children=True,
                    primary_placement=True,
                    visibility_status="visible",
                    nav_summary=None,
                    created_by_actor_type="system",
                    created_by_actor_id=user_id,
                    updated_at=now,
                    created_at=now,
                )
            )
        policy = connection.execute(
            sa.select(tree_policies.c.workspace_id).where(
                tree_policies.c.workspace_id == workspace_id,
                tree_policies.c.section == section,
            )
        ).scalar_one_or_none()
        if policy is None:
            connection.execute(
                tree_policies.insert().values(
                    workspace_id=workspace_id,
                    section=section,
                    allow_user_folder_create=True,
                    allow_page_as_parent=True,
                    fixed_root_node_ids=[str(root_id)],
                    inherit_permissions_from_parent=False,
                    inherit_review_policy_from_parent=False,
                    max_depth_warning_threshold=8,
                )
            )
        else:
            connection.execute(
                tree_policies.update()
                .where(
                    tree_policies.c.workspace_id == workspace_id,
                    tree_policies.c.section == section,
                )
                .values(fixed_root_node_ids=[str(root_id)])
            )


def _migration_default_review_policy() -> dict[str, object]:
    return {
        "preset": "balanced",
        "policy_version": "2026-05-31.balanced.v1",
        "defaults": {
            "context_draft_create": "low",
            "context_partial_update": "low",
            "context_full_rewrite": "medium",
            "context_publish": "medium",
            "context_ingress_auto": "low",
            "context_ingress_review_required": "medium",
            "direct_page_write_audit": "low",
            "stale_alternative": "medium",
            "inline_comment": "low",
            "agent_sensitive_draft": "high",
            "agent_non_sensitive_draft": "low",
            "agent_sensitive_activation": "high",
            "agent_non_sensitive_activation": "low",
            "tree_mutation": "low",
            "source_deleted_notice": "low",
        },
        "risk_rules": {
            "raise_one_level": [
                "manual_citation",
                "decision_page_type",
                "low_placement_confidence",
            ],
            "force_high": [
                "citation_warning",
                "risk_notes_present",
                "sensitive_agent_category",
            ],
        },
        "review_required": {
            "from_risk": "medium",
            "always_for_sensitive_agent": True,
            "always_for_decision_page": True,
            "always_for_ai_authored": True,
        },
        "automation": {
            "auto_approve_enabled": False,
            "auto_approve_below": "medium",
            "auto_approve_after_hours": 168,
            "auto_approve_by_risk": {
                "low": {"allowed": False, "delay_days": 7},
                "medium": {"allowed": False, "delay_days": 14},
                "high": {"allowed": False, "delay_days": 28},
            },
        },
    }


def _migration_uuid_value(connection: Connection, value: UUID):
    if connection.dialect.name == "sqlite":
        return value.hex
    return value
