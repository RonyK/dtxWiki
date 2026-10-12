"""CTX-02: Context-history migration normalization and forward convergence.

The revision-0004 cases drive the real Alembic chain from a 0003-era SQLite
database (not the ORM's current head schema), so those fixtures use raw Core
reflection. The revision-0007 repair regression starts from the 0006 schema
and upgrades through the current migration head, so it can safely use the
matching ORM models for its seed data.
"""

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url

from backend.tests.utils.migrated_sqlite_templates import shared_templates

ACTOR_ID = "00000000-0000-0000-0000-000000000010"
workspace_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.workspace_schema"
)


def _database_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{tmp_path / name}"


def _run_steps(engine, *steps) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for step in steps:
                step()


@pytest.fixture(scope="session")
def sqlite_templates(tmp_path_factory):
    return shared_templates(tmp_path_factory)


def _clone_workspace_member_boundary(templates, database_url: str):
    """Engine on a copy of 0001 plus ``_upgrade_base_schema``."""
    templates.clone("boundary_workspace_member", Path(make_url(database_url).database))
    return create_engine(database_url)


def _insert_workspace(connection, workspaces, *, workspace_id, actor_id, slug, now):
    values = {
        "id": workspace_id.hex,
        "slug": slug,
        "title": "Migration Normalization Workspace",
        "name": "Migration Normalization Workspace",
        "description": None,
        "goal": "Migration coverage",
        "default_language": "en",
        "library_policy": {},
        "write_policy": {},
        "citation_policy": {},
        "agent_injection_policy": {},
        "review_policy": {},
        "lint_policy": {},
        "auto_fix_enabled": False,
        "visibility": "private",
        "created_by_actor_id": actor_id.hex,
        "owner_actor_id": actor_id.hex,
        "owner_user_id": None,
        "updated_at": now,
        "created_at": now,
    }
    if "directory_title_key" in workspaces.c:
        values["directory_title_key"] = "migration normalization workspace"
    connection.execute(workspaces.insert().values(**values))


def _insert_page(connection, pages, *, page_id, workspace_id, slug, now):
    connection.execute(
        pages.insert().values(
            id=page_id.hex,
            workspace_id=workspace_id.hex,
            slug=slug,
            title="Migration Normalization",
            page_type="overview",
            tags=[],
            aliases=[],
            created_from_ask_turn_ids=[],
            lifecycle_status="published",
            updated_at=now,
            created_at=now,
        )
    )


def _insert_version(
    connection,
    versions,
    *,
    version_id,
    page_id,
    parent_version_id,
    version_no,
    version_status,
    graph_status,
    now,
):
    connection.execute(
        versions.insert().values(
            id=version_id.hex,
            page_id=page_id.hex,
            parent_version_id=(
                parent_version_id.hex if parent_version_id is not None else None
            ),
            version_no=version_no,
            body_md=f"# Migration Normalization\n\nv{version_no}",
            rendering_profile="gfm_mermaid",
            content_hash=str(version_no) * 64,
            graph_status=graph_status,
            actor_type="human",
            version_status=version_status,
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
            created_at=now,
        )
    )


def test_migration_demotes_pre_publish_orphan_linear_draft(
    tmp_path: Path, sqlite_templates
):
    """A linear draft older than the published head (by `version_no`), with
    the page's `latest_draft_version_id` pointer unset, can only be a
    pre-publish orphan left behind by the (pre-fix)
    `apply_context_publication`. Revision 0004 must demote it before its
    preflight runs, not refuse the upgrade.

    The single-publish case: the orphan draft is the published head's direct
    parent (nothing yet stands between them). See
    `test_migration_demotes_pre_publish_orphan_stranded_behind_a_second_
    publish` below for the case that matters most in practice -- a page
    published more than once, where the orphan sits behind an intervening
    `superseded_snapshot` row.
    """

    database_url = _database_url(tmp_path, "ctx02-orphan.db")
    engine = _clone_workspace_member_boundary(sqlite_templates, database_url)
    workspace_id = uuid4()
    actor_id = uuid4()
    page_id = uuid4()
    old_head_id = uuid4()
    ancestor_id = uuid4()
    new_head_id = uuid4()
    now = datetime.now(UTC)
    try:
        metadata = sa.MetaData()
        workspaces = sa.Table("workspace", metadata, autoload_with=engine)
        pages = sa.Table("context_page", metadata, autoload_with=engine)
        versions = sa.Table("context_page_version", metadata, autoload_with=engine)
        with engine.begin() as connection:
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=workspace_id,
                actor_id=actor_id,
                slug="ctx02-orphan-workspace",
                now=now,
            )
            _insert_page(
                connection,
                pages,
                page_id=page_id,
                workspace_id=workspace_id,
                slug="ctx02-orphan-page",
                now=now,
            )
            # The true previous published head -- must stay untouched
            # (`version_status` is not `draft`, so it never matches the
            # UPDATE's WHERE clause regardless of `version_no`).
            _insert_version(
                connection,
                versions,
                version_id=old_head_id,
                page_id=page_id,
                parent_version_id=None,
                version_no=1,
                version_status="superseded_snapshot",
                graph_status="linear",
                now=now,
            )
            # The pre-publish-orphan ancestor: a linear draft between the old
            # and new published heads, with no pointer naming it.
            _insert_version(
                connection,
                versions,
                version_id=ancestor_id,
                page_id=page_id,
                parent_version_id=old_head_id,
                version_no=2,
                version_status="draft",
                graph_status="linear",
                now=now,
            )
            _insert_version(
                connection,
                versions,
                version_id=new_head_id,
                page_id=page_id,
                parent_version_id=ancestor_id,
                version_no=3,
                version_status="published_snapshot",
                graph_status="linear",
                now=now,
            )
            connection.execute(
                pages.update()
                .where(pages.c.id == page_id.hex)
                .values(
                    current_version_id=new_head_id.hex,
                    current_published_version_id=new_head_id.hex,
                    latest_draft_version_id=None,
                )
            )

        # Reproduce the failure this fixes: at HEAD, the migration's own
        # preflight counts this page before any normalization runs.
        with engine.connect() as verify_connection:
            counts_before = workspace_phase._context_history_preflight_counts(
                verify_connection
            )
        assert counts_before["latest_draft_unset_with_live_draft"] == 1

        # Without the fix, this raises RuntimeError("context history preflight
        # failed ...") and the database stays pinned at 0003.
        _run_steps(engine, workspace_phase.upgrade)

        engine.dispose()
        engine = create_engine(database_url)
        metadata = sa.MetaData()
        versions = sa.Table("context_page_version", metadata, autoload_with=engine)
        with engine.connect() as connection:
            ancestor_row = connection.execute(
                select(versions.c.version_status, versions.c.graph_status).where(
                    versions.c.id == ancestor_id.hex
                )
            ).one()
            old_head_row = connection.execute(
                select(versions.c.version_status, versions.c.graph_status).where(
                    versions.c.id == old_head_id.hex
                )
            ).one()
        assert ancestor_row.version_status == "draft"
        assert ancestor_row.graph_status == "discarded"
        assert old_head_row.version_status == "superseded_snapshot"
        assert old_head_row.graph_status == "linear"
    finally:
        engine.dispose()


def test_migration_demotes_pre_publish_orphan_stranded_behind_a_second_publish(
    tmp_path: Path, sqlite_templates
):
    """The case that matters most in practice: a page published TWICE.

    Publish #1 (pre-fix) leaves an orphan draft `v1` behind (`version_no=1`,
    `parent_version_id=NULL` -- it was the page's very first version) while
    its direct successor `v2` becomes the published head. Later, a fresh
    single-level draft `v3` (forked straight from `v2`, no intermediate
    draft of its own) gets published; publish #2 demotes `v2` to
    `superseded_snapshot` and clears the draft pointer again. `v1` has been
    stranded the entire time, but it is now two hops behind the head, with a
    `superseded_snapshot` row (`v2`) sitting between them.

    A backward parent-pointer walk from the head would reach `v2`, find it
    is not `draft`/`linear`, and stop -- never reaching `v1`. This is
    exactly why the migration uses `version_no < head.version_no` instead of
    a walk: `v1.version_no (1) < v3.version_no (3)` regardless of how many
    publishes and superseded rows stand in between.
    """

    database_url = _database_url(tmp_path, "ctx02-second-publish-orphan.db")
    engine = _clone_workspace_member_boundary(sqlite_templates, database_url)
    workspace_id = uuid4()
    actor_id = uuid4()
    page_id = uuid4()
    v1_orphan_id = uuid4()
    v2_superseded_id = uuid4()
    v3_head_id = uuid4()
    now = datetime.now(UTC)
    try:
        metadata = sa.MetaData()
        workspaces = sa.Table("workspace", metadata, autoload_with=engine)
        pages = sa.Table("context_page", metadata, autoload_with=engine)
        versions = sa.Table("context_page_version", metadata, autoload_with=engine)
        with engine.begin() as connection:
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=workspace_id,
                actor_id=actor_id,
                slug="ctx02-second-publish-workspace",
                now=now,
            )
            _insert_page(
                connection,
                pages,
                page_id=page_id,
                workspace_id=workspace_id,
                slug="ctx02-second-publish-page",
                now=now,
            )
            # Orphan from publish #1: the page's very first version, left
            # `draft`/`linear` by the pre-fix publish.
            _insert_version(
                connection,
                versions,
                version_id=v1_orphan_id,
                page_id=page_id,
                parent_version_id=None,
                version_no=1,
                version_status="draft",
                graph_status="linear",
                now=now,
            )
            # The FIRST published head, later demoted by publish #2. This
            # row is what defeats a parent-pointer walk from the current
            # head: it sits directly between v3 and the orphan.
            _insert_version(
                connection,
                versions,
                version_id=v2_superseded_id,
                page_id=page_id,
                parent_version_id=v1_orphan_id,
                version_no=2,
                version_status="superseded_snapshot",
                graph_status="linear",
                now=now,
            )
            # The current published head, from publish #2.
            _insert_version(
                connection,
                versions,
                version_id=v3_head_id,
                page_id=page_id,
                parent_version_id=v2_superseded_id,
                version_no=3,
                version_status="published_snapshot",
                graph_status="linear",
                now=now,
            )
            connection.execute(
                pages.update()
                .where(pages.c.id == page_id.hex)
                .values(
                    current_version_id=v3_head_id.hex,
                    current_published_version_id=v3_head_id.hex,
                    latest_draft_version_id=None,
                )
            )

        with engine.connect() as verify_connection:
            counts_before = workspace_phase._context_history_preflight_counts(
                verify_connection
            )
        assert counts_before["latest_draft_unset_with_live_draft"] == 1

        # Without the fix (or with the earlier parent-walk version of it),
        # this raises RuntimeError and the database stays pinned at 0003.
        _run_steps(engine, workspace_phase.upgrade)

        engine.dispose()
        engine = create_engine(database_url)
        metadata = sa.MetaData()
        versions = sa.Table("context_page_version", metadata, autoload_with=engine)
        with engine.connect() as connection:
            v1_row = connection.execute(
                select(versions.c.version_status, versions.c.graph_status).where(
                    versions.c.id == v1_orphan_id.hex
                )
            ).one()
            v2_row = connection.execute(
                select(versions.c.version_status, versions.c.graph_status).where(
                    versions.c.id == v2_superseded_id.hex
                )
            ).one()
        assert v1_row.version_status == "draft"
        assert v1_row.graph_status == "discarded"
        # The already-superseded row in between is untouched: it was never
        # `draft` to begin with, so the UPDATE's WHERE clause never matches
        # it regardless of `version_no`.
        assert v2_row.version_status == "superseded_snapshot"
        assert v2_row.graph_status == "linear"
    finally:
        engine.dispose()


def test_migration_preflight_still_blocks_live_unset_drafts_newer_than_head(
    tmp_path: Path, sqlite_templates
):
    """Two linear drafts, both newer than the published head by `version_no`,
    with the pointer unset: genuine ambiguity (which of the two is the edit
    head?), not a pre-publish orphan. `_demote_pre_publish_orphan_drafts`
    only demotes rows with `version_no < head.version_no`, so it can never
    touch either -- the upgrade must still refuse and send the operator
    through the documented repair command.
    """

    database_url = _database_url(tmp_path, "ctx02-ambiguous.db")
    engine = _clone_workspace_member_boundary(sqlite_templates, database_url)
    workspace_id = uuid4()
    actor_id = uuid4()
    page_id = uuid4()
    head_id = uuid4()
    draft_a_id = uuid4()
    draft_b_id = uuid4()
    now = datetime.now(UTC)
    try:
        metadata = sa.MetaData()
        workspaces = sa.Table("workspace", metadata, autoload_with=engine)
        pages = sa.Table("context_page", metadata, autoload_with=engine)
        versions = sa.Table("context_page_version", metadata, autoload_with=engine)
        with engine.begin() as connection:
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=workspace_id,
                actor_id=actor_id,
                slug="ctx02-ambiguous-workspace",
                now=now,
            )
            _insert_page(
                connection,
                pages,
                page_id=page_id,
                workspace_id=workspace_id,
                slug="ctx02-ambiguous-page",
                now=now,
            )
            _insert_version(
                connection,
                versions,
                version_id=head_id,
                page_id=page_id,
                parent_version_id=None,
                version_no=1,
                version_status="published_snapshot",
                graph_status="linear",
                now=now,
            )
            # Two live drafts, both newer than the head, no pointer naming
            # either -- the normalization UPDATE's `version_no < head.version_no`
            # guard must leave both alone.
            _insert_version(
                connection,
                versions,
                version_id=draft_a_id,
                page_id=page_id,
                parent_version_id=head_id,
                version_no=2,
                version_status="draft",
                graph_status="linear",
                now=now,
            )
            _insert_version(
                connection,
                versions,
                version_id=draft_b_id,
                page_id=page_id,
                parent_version_id=head_id,
                version_no=3,
                version_status="draft",
                graph_status="linear",
                now=now,
            )
            connection.execute(
                pages.update()
                .where(pages.c.id == page_id.hex)
                .values(
                    current_version_id=head_id.hex,
                    current_published_version_id=head_id.hex,
                    latest_draft_version_id=None,
                )
            )

        with pytest.raises(RuntimeError, match="latest_draft_unset_with_live_draft=1"):
            _run_steps(engine, workspace_phase.upgrade)
    finally:
        engine.dispose()


def test_migration_upgrade_head_preserves_a_rejected_restartable_draft(
    tmp_path: Path, sqlite_templates
):
    """Adversarial-review follow-up to CTX-02 (post-3f40a6c7): the
    `version_no` heuristic above cannot tell a true pre-fix orphan from a
    REJECTED-and-not-yet-restarted review draft -- rejecting a review
    deliberately clears `latest_draft_version_id` while leaving the
    rejected version `draft`/`linear` (``review_service.py``'s
    ``decide_review``, so ``update_draft``'s ``restart_after_reject`` path
    can fork a fresh draft from it later), which is exactly the pointer-
    unset-plus-linear-draft shape this migration sweeps up. Without the
    exclusion, running the REAL `alembic upgrade head` chain against a
    database in this shape either fails outright (no second publish yet:
    `latest_draft_unset_with_live_draft` has no `version_no` floor at all)
    or silently discards deliberately-preserved restart material (a second,
    unrelated publish happened first, as staged here -- ordinary, frequent
    activity, not a rare edge case).

    Unlike the other fixtures in this file, this one also seeds
    `change_set`/`change_operation`/`review_item` rows (present since
    0001, untouched by 0004's DDL) to reproduce the exact join
    `_rejected_restartable_version_ids` (backend/migrations/versions/0004_
    workspace_schema.py) performs.
    """

    database_url = _database_url(tmp_path, "ctx02-rejected-restartable.db")
    engine = _clone_workspace_member_boundary(sqlite_templates, database_url)
    workspace_id = uuid4()
    actor_id = uuid4()
    page_id = uuid4()
    old_head_id = uuid4()
    rejected_draft_id = uuid4()
    new_head_id = uuid4()
    change_set_id = uuid4()
    change_operation_id = uuid4()
    review_item_id = uuid4()
    now = datetime.now(UTC)
    try:
        metadata = sa.MetaData()
        workspaces = sa.Table("workspace", metadata, autoload_with=engine)
        pages = sa.Table("context_page", metadata, autoload_with=engine)
        versions = sa.Table("context_page_version", metadata, autoload_with=engine)
        change_sets = sa.Table("change_set", metadata, autoload_with=engine)
        change_operations = sa.Table("change_operation", metadata, autoload_with=engine)
        review_items = sa.Table("review_item", metadata, autoload_with=engine)
        with engine.begin() as connection:
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=workspace_id,
                actor_id=actor_id,
                slug="ctx02-rejected-restartable-workspace",
                now=now,
            )
            _insert_page(
                connection,
                pages,
                page_id=page_id,
                workspace_id=workspace_id,
                slug="ctx02-rejected-restartable-page",
                now=now,
            )
            # The published head at the time the draft below was rejected --
            # now superseded by an unrelated later publish (`new_head_id`).
            _insert_version(
                connection,
                versions,
                version_id=old_head_id,
                page_id=page_id,
                parent_version_id=None,
                version_no=1,
                version_status="superseded_snapshot",
                graph_status="linear",
                now=now,
            )
            # The rejected draft: `draft`/`linear`, no pointer names it
            # (exactly what `decide_review`'s reject path leaves behind),
            # `version_no` now behind the head below because of the
            # unrelated publish -- indistinguishable from true dead history
            # by `version_no` alone.
            _insert_version(
                connection,
                versions,
                version_id=rejected_draft_id,
                page_id=page_id,
                parent_version_id=old_head_id,
                version_no=2,
                version_status="draft",
                graph_status="linear",
                now=now,
            )
            # An UNRELATED later edit, forked straight from `old_head_id`
            # (NOT a restart of the rejected draft), published over it.
            _insert_version(
                connection,
                versions,
                version_id=new_head_id,
                page_id=page_id,
                parent_version_id=old_head_id,
                version_no=3,
                version_status="published_snapshot",
                graph_status="linear",
                now=now,
            )
            connection.execute(
                pages.update()
                .where(pages.c.id == page_id.hex)
                .values(
                    current_version_id=new_head_id.hex,
                    current_published_version_id=new_head_id.hex,
                    latest_draft_version_id=None,
                )
            )
            connection.execute(
                change_sets.insert().values(
                    id=change_set_id.hex,
                    workspace_id=workspace_id.hex,
                    reason="initial MCP draft",
                    trigger_type="mcp_write_draft",
                    actor_type="mcp_client",
                    actor_id=actor_id.hex,
                    origin_chat_session_id=None,
                    status="rejected",
                    risk_level="low",
                    cite_call_skipped=False,
                    updated_at=now,
                    created_at=now,
                )
            )
            connection.execute(
                change_operations.insert().values(
                    id=change_operation_id.hex,
                    change_set_id=change_set_id.hex,
                    target_type="context_page",
                    target_id=page_id.hex,
                    operation="update",
                    diff_summary=None,
                    # The canonical hyphenated `str(uuid)` form every write
                    # path builds via f-string (context_write_service.py
                    # etc.) -- deliberately NOT `.hex`, to reproduce the
                    # on-disk-format mismatch `_rejected_restartable_
                    # version_ids` exists to bridge.
                    diff_ref=f"context_page_version:{rejected_draft_id}",
                    created_at=now,
                )
            )
            connection.execute(
                review_items.insert().values(
                    id=review_item_id.hex,
                    workspace_id=workspace_id.hex,
                    change_set_id=change_set_id.hex,
                    review_type="context_update",
                    status="rejected",
                    risk_level="low",
                    risk_policy_version=None,
                    risk_resolution_summary=None,
                    risk_signals=None,
                    risk_policy_snapshot=None,
                    due_at=None,
                    decision_summary="not ready",
                    claimed_by=None,
                    claimed_at=None,
                    updated_at=now,
                    created_at=now,
                )
            )

        with engine.connect() as verify_connection:
            counts_before = workspace_phase._context_history_preflight_counts(
                verify_connection
            )
        # The bare existence of the rejected-not-yet-restarted draft must
        # not count as a blocker, with or without the unrelated publish.
        assert counts_before["latest_draft_unset_with_live_draft"] == 0

        # Without the fix, this raises RuntimeError (no version_no floor on
        # this count) or -- with only the version_no exclusion and not the
        # preflight-count exclusion -- would still be blocked here even
        # though the row itself would already be spared.
        _run_steps(engine, workspace_phase.upgrade)

        engine.dispose()
        engine = create_engine(database_url)
        metadata = sa.MetaData()
        versions = sa.Table("context_page_version", metadata, autoload_with=engine)
        with engine.connect() as connection:
            rejected_row = connection.execute(
                select(versions.c.version_status, versions.c.graph_status).where(
                    versions.c.id == rejected_draft_id.hex
                )
            ).one()
        # The decisive assertion: still `draft`/`linear`, not `discarded`.
        assert rejected_row.version_status == "draft"
        assert rejected_row.graph_status == "linear"
    finally:
        engine.dispose()
