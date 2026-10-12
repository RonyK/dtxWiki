from importlib import import_module
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from typer.testing import CliRunner

from backend.app.db import make_engine, make_session_factory
from backend.cli import main as cli_main
from backend.models.base import Base
from backend.models.change import ChangeSet
from backend.models.context import ContextCitation, ContextPage, ContextPageVersion
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_lifecycle import audit_context_history

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def test_context_history_repair_dry_run_reports_without_mutation(tmp_path: Path):
    database_url, session_factory, page_id, head_id, rogue_id = _repairable_database(
        tmp_path
    )
    before = _history_snapshot(session_factory, page_id)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "context",
            "history",
            "repair",
            "--dry-run",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 1
    assert "context history dry-run" in result.stdout
    assert "violations=4 repairable=4 blocking=0" in result.stdout
    assert "published_head_status_mismatch" in result.stdout
    assert "published_head_graph_mismatch" in result.stdout
    assert "published_head_marked_as_draft_head" in result.stdout
    assert "extra_published_snapshot" in result.stdout
    assert "No changes applied." in result.stdout
    assert str(head_id) in result.stdout
    assert str(rogue_id) in result.stdout
    assert _history_snapshot(session_factory, page_id) == before


def test_context_history_repair_apply_uses_canonical_lineage(tmp_path: Path):
    database_url, session_factory, page_id, head_id, rogue_id = _repairable_database(
        tmp_path
    )
    preserved_before = _preserved_history_evidence_snapshot(
        session_factory,
        page_id,
    )

    result = CliRunner().invoke(
        cli_main.app,
        [
            "context",
            "history",
            "repair",
            "--apply",
            "--db",
            database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Take a verified database backup" in result.stderr
    assert "violations=0 repairable=0 blocking=0" in result.stdout
    assert "pages=1 versions=2" in result.stdout
    preserved_after = _preserved_history_evidence_snapshot(
        session_factory,
        page_id,
    )
    assert (
        preserved_after["page_non_pointer_fields"]
        == preserved_before["page_non_pointer_fields"]
    )
    assert (
        preserved_after["version_non_status_fields"]
        == preserved_before["version_non_status_fields"]
    )
    assert (
        preserved_after["version_bodies_hashes_numbers"]
        == preserved_before["version_bodies_hashes_numbers"]
    )
    assert preserved_after["citations"] == preserved_before["citations"]
    assert preserved_after["change_sets"] == preserved_before["change_sets"]
    assert preserved_after["review_decisions"] == preserved_before["review_decisions"]
    page_state, versions = _history_snapshot(session_factory, page_id)
    assert page_state == (head_id, head_id, None)
    assert versions[head_id] == ("published_snapshot", "linear")
    assert versions[rogue_id] == ("superseded_snapshot", "linear")


def test_context_history_repair_apply_is_idempotent(tmp_path: Path):
    database_url, session_factory, page_id, _head_id, _rogue_id = _repairable_database(
        tmp_path
    )
    runner = CliRunner()
    args = [
        "context",
        "history",
        "repair",
        "--apply",
        "--db",
        database_url,
    ]
    first = runner.invoke(cli_main.app, args)
    assert first.exit_code == 0, first.output
    after_first = _history_snapshot(session_factory, page_id)

    second = runner.invoke(cli_main.app, args)
    default_dry_run = runner.invoke(
        cli_main.app,
        ["context", "history", "repair", "--db", database_url],
    )

    assert second.exit_code == 0, second.output
    assert "pages=0 versions=0" in second.stdout
    assert default_dry_run.exit_code == 0, default_dry_run.output
    assert "context history dry-run" in default_dry_run.stdout
    assert "violations=0" in default_dry_run.stdout
    assert "No changes applied." in default_dry_run.stdout
    assert _history_snapshot(session_factory, page_id) == after_first


def test_context_history_repair_refuses_ambiguous_apply_and_uses_exit_codes(
    tmp_path: Path,
):
    (
        database_url,
        session_factory,
        page_id,
        _version_id,
        repairable_page_id,
    ) = _blocked_database(tmp_path)
    before = (
        _history_snapshot(session_factory, page_id),
        _history_snapshot(session_factory, repairable_page_id),
    )
    runner = CliRunner()
    base_args = ["context", "history", "repair", "--db", database_url]

    dry_run = runner.invoke(cli_main.app, [*base_args, "--dry-run"])
    apply = runner.invoke(cli_main.app, [*base_args, "--apply"])

    assert dry_run.exit_code == 1
    assert "published_head_cross_page" in dry_run.stdout
    assert "current_version_cross_page" in dry_run.stdout
    assert "latest_draft_cross_page" in dry_run.stdout
    # The blocked page also never had a current pointer written, and its
    # published pointer is cross-page, so nothing proves an edit head for it.
    assert "current_version_unset" in dry_run.stdout
    assert "blocking=4" in dry_run.stdout
    assert apply.exit_code == 2
    assert "apply-refused" in apply.stdout
    assert "no changes were applied" in apply.stderr
    assert (
        _history_snapshot(session_factory, page_id),
        _history_snapshot(session_factory, repairable_page_id),
    ) == before


def test_context_history_repair_apply_is_scoped_per_page(tmp_path: Path):
    """CTX-04: an unrepairable page must not withhold --apply from a page
    whose own violations are all repairable.

    Before this fix, `repair_context_history` audited and gated the whole
    transaction at once, so one page's blocking violation refused --apply for
    every page in the database. The all-blocked case above (both pages carry
    their own unrepairable violation) already exercised that gate and still
    does; this is the mixed case nothing here previously covered -- one
    genuinely blocked page and one independently repairable page in the same
    database.
    """

    (
        database_url,
        session_factory,
        blocked_page_id,
        repairable_page_id,
    ) = _mixed_repairable_and_blocked_database(tmp_path)
    before_blocked = _history_snapshot(session_factory, blocked_page_id)

    result = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", database_url],
    )

    assert result.exit_code == 1, result.output
    assert "pages=1 versions=0" in result.stdout
    assert "1 page(s) remain ambiguous" in result.stderr

    # The blocked page's cross-page corruption is untouched...
    assert _history_snapshot(session_factory, blocked_page_id) == before_blocked
    # ...but the independently repairable page's fix was NOT rolled back by
    # the blocked page sharing the transaction. Reload through a fresh
    # session/connection (_history_snapshot always does) so a rollback that
    # only looked committed in-process cannot hide here.
    page_state, _versions = _history_snapshot(session_factory, repairable_page_id)
    current_id, published_id, latest_draft_id = page_state
    assert current_id == published_id
    assert latest_draft_id is None
    with session_factory() as session:
        repairable_page = session.get(ContextPage, repairable_page_id)
        assert repairable_page is not None
        assert repairable_page.lifecycle_status == "published"


def test_context_history_repair_clears_migration_preflight_blockers(tmp_path: Path):
    database_url, session_factory, page_id, head_id = (
        _migration_preflight_repairable_database(tmp_path)
    )
    runner = CliRunner()

    dry_run = runner.invoke(
        cli_main.app,
        ["context", "history", "repair", "--dry-run", "--db", database_url],
    )
    applied = runner.invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", database_url],
    )

    assert dry_run.exit_code == 1, dry_run.output
    assert "published_without_head" in dry_run.stdout
    assert "current_version_status_contradiction" in dry_run.stdout
    assert "latest_draft_status_contradiction" in dry_run.stdout
    assert "blocking=0" in dry_run.stdout
    assert applied.exit_code == 0, applied.output
    page_state, _versions = _history_snapshot(session_factory, page_id)
    assert page_state == (head_id, head_id, None)
    migration = import_module(
        "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.workspace_schema"
    )
    engine = session_factory.kw["bind"]
    with engine.connect() as connection:
        assert not any(migration._context_history_preflight_counts(connection).values())


def _repairable_database(tmp_path: Path):
    database_url, session_factory, workspace_id = _empty_database(
        tmp_path / "repairable.db"
    )
    with session_factory.begin() as session:
        page = _page(workspace_id, slug="repairable-history")
        session.add(page)
        session.flush()
        head = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=1,
            status="superseded_snapshot",
            graph_status="branched",
        )
        session.add(head)
        session.flush()
        rogue = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=2,
            status="published_snapshot",
            graph_status="linear",
            parent_version_id=head.id,
        )
        session.add(rogue)
        session.flush()
        change_set = ChangeSet(
            workspace_id=workspace_id,
            reason="Preserved history evidence",
            trigger_type="test_seed",
            actor_type="human",
            actor_id=UUID(ACTOR_ID),
            status="applied",
            risk_level="low",
        )
        session.add(change_set)
        session.flush()
        head.change_set_id = change_set.id
        citation = ContextCitation(
            workspace_id=workspace_id,
            target_kind="context_page_version",
            target_id=head.id,
            target_location={"section": "body"},
            cited_context_page_version_id=rogue.id,
            location={"paragraph": 1},
            snippet="Preserved citation evidence",
            citation_type="context_page",
            verification_status="verified",
            created_via="manual",
            created_by_actor_type="human",
        )
        review = ReviewItem(
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            review_type="context_update",
            status="approved",
            risk_level="low",
            decision_summary="Preserved review evidence",
        )
        session.add_all([citation, review])
        session.flush()
        session.add(
            ReviewDecision(
                workspace_id=workspace_id,
                review_item_id=review.id,
                decision_type="approve",
                actor_type="human",
                actor_id=UUID(ACTOR_ID),
                decision_reason="Preserve the published evidence",
                resulting_review_status="approved",
                resulting_change_set_status="applied",
            )
        )
        page.current_version_id = head.id
        page.current_published_version_id = head.id
        page.latest_draft_version_id = head.id
        page.lifecycle_status = "published"
    return database_url, session_factory, page.id, head.id, rogue.id


def _blocked_database(tmp_path: Path):
    database_url, session_factory, workspace_id = _empty_database(
        tmp_path / "blocked.db"
    )
    with session_factory.begin() as session:
        page = _page(workspace_id, slug="blocked-history")
        session.add(page)
        session.flush()
        version = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=1,
            status="published_snapshot",
            graph_status="linear",
        )
        session.add(version)
        session.flush()
        page.lifecycle_status = "published"

        repairable_page = _page(workspace_id, slug="repairable-but-blocked")
        session.add(repairable_page)
        session.flush()
        repairable_version = _version(
            repairable_page.id,
            workspace_id=workspace_id,
            version_no=1,
            status="superseded_snapshot",
            graph_status="linear",
        )
        session.add(repairable_version)
        session.flush()
        page.current_published_version_id = repairable_version.id
        repairable_page.current_version_id = version.id
        repairable_page.current_published_version_id = repairable_version.id
        repairable_page.latest_draft_version_id = version.id
        repairable_page.lifecycle_status = "published"
    return (
        database_url,
        session_factory,
        page.id,
        version.id,
        repairable_page.id,
    )


def _mixed_repairable_and_blocked_database(tmp_path: Path):
    """One page with an unrepairable cross-page pointer, and a second,
    independent page with a single deterministically repairable violation.
    """

    database_url, session_factory, workspace_id = _empty_database(
        tmp_path / "mixed-repair-block.db"
    )
    with session_factory.begin() as session:
        blocked_page = _page(workspace_id, slug="ctx04-blocked")
        repairable_page = _page(workspace_id, slug="ctx04-repairable")
        session.add_all([blocked_page, repairable_page])
        session.flush()
        blocked_version = _version(
            blocked_page.id,
            workspace_id=workspace_id,
            version_no=1,
            status="published_snapshot",
            graph_status="linear",
        )
        repairable_version = _version(
            repairable_page.id,
            workspace_id=workspace_id,
            version_no=1,
            status="published_snapshot",
            graph_status="linear",
        )
        session.add_all([blocked_version, repairable_version])
        session.flush()

        blocked_page.current_published_version_id = blocked_version.id
        # Cross-page: names a version that belongs to `repairable_page`, the
        # one violation code the audit never treats as repairable.
        blocked_page.current_version_id = repairable_version.id
        blocked_page.lifecycle_status = "published"

        repairable_page.current_published_version_id = repairable_version.id
        repairable_page.current_version_id = repairable_version.id
        # Redundant overlap with no live draft behind it -- the one class
        # `_repair_context_head_pointers` clears deterministically.
        repairable_page.latest_draft_version_id = repairable_version.id
        repairable_page.lifecycle_status = "published"
    return database_url, session_factory, blocked_page.id, repairable_page.id


def _migration_preflight_repairable_database(tmp_path: Path):
    database_url, session_factory, workspace_id = _empty_database(
        tmp_path / "preflight-repairable.db"
    )
    with session_factory.begin() as session:
        page = _page(workspace_id, slug="preflight-repairable")
        other = _page(workspace_id, slug="preflight-other")
        session.add_all([page, other])
        session.flush()
        head = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=1,
            status="published_snapshot",
            graph_status="linear",
        )
        other_head = _version(
            other.id,
            workspace_id=workspace_id,
            version_no=1,
            status="published_snapshot",
            graph_status="linear",
        )
        session.add_all([head, other_head])
        session.flush()
        page.current_version_id = head.id
        page.latest_draft_version_id = head.id
        page.lifecycle_status = "published"
        other.current_version_id = other_head.id
        other.current_published_version_id = other_head.id
        other.lifecycle_status = "published"
    return database_url, session_factory, page.id, head.id


def _empty_database(path: Path):
    database_url = f"sqlite+pysqlite:///{path}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        seed_default_workspace(session)
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        workspace_id = workspace.id
    return database_url, session_factory, workspace_id


def _page(workspace_id: UUID, *, slug: str) -> ContextPage:
    return ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug=slug,
        title="Context History Repair",
        page_type="overview",
        tags=[],
        aliases=[],
        lifecycle_status="draft",
    )


def _version(
    page_id: UUID,
    *,
    workspace_id: UUID,
    version_no: int,
    status: str,
    graph_status: str,
    parent_version_id: UUID | None = None,
) -> ContextPageVersion:
    return ContextPageVersion(
        id=uuid4(),
        workspace_id=workspace_id,
        page_id=page_id,
        parent_version_id=parent_version_id,
        version_no=version_no,
        body_md=f"# Context History Repair\n\nv{version_no}",
        content_hash=str(version_no) * 64,
        graph_status=graph_status,
        actor_type="human",
        actor_id=UUID(ACTOR_ID),
        version_status=status,
        created_by_actor_kind="human",
        created_by_actor_id=ACTOR_ID,
    )


def _history_snapshot(session_factory, page_id: UUID):
    with session_factory() as session:
        page = session.get(ContextPage, page_id)
        assert page is not None
        versions = session.scalars(
            select(ContextPageVersion).where(ContextPageVersion.page_id == page_id)
        ).all()
        return (
            (
                page.current_version_id,
                page.current_published_version_id,
                page.latest_draft_version_id,
            ),
            {
                version.id: (version.version_status, version.graph_status)
                for version in versions
            },
        )


def _preserved_history_evidence_snapshot(session_factory, page_id: UUID):
    """Capture every persisted field that the repair contract preserves."""

    with session_factory() as session:
        page_columns = [
            column.name
            for column in ContextPage.__table__.columns
            if column.name
            not in {
                "current_version_id",
                "current_published_version_id",
                "latest_draft_version_id",
                # SQLAlchemy advances this bookkeeping timestamp when a pointer
                # changes; it is not Context content or review evidence.
                "updated_at",
            }
        ]
        version_columns = [
            column.name
            for column in ContextPageVersion.__table__.columns
            if column.name not in {"version_status", "graph_status"}
        ]
        return {
            "page_non_pointer_fields": _rows_snapshot(
                session,
                ContextPage,
                page_columns,
                where=(ContextPage.id == page_id),
            ),
            # Includes body_md, content_hash, version_no, lineage and authorship.
            "version_non_status_fields": _rows_snapshot(
                session,
                ContextPageVersion,
                version_columns,
                where=(ContextPageVersion.page_id == page_id),
            ),
            "version_bodies_hashes_numbers": _rows_snapshot(
                session,
                ContextPageVersion,
                ["id", "body_md", "content_hash", "version_no"],
                where=(ContextPageVersion.page_id == page_id),
            ),
            "citations": _rows_snapshot(
                session,
                ContextCitation,
                [column.name for column in ContextCitation.__table__.columns],
            ),
            "change_sets": _rows_snapshot(
                session,
                ChangeSet,
                [column.name for column in ChangeSet.__table__.columns],
            ),
            "review_decisions": _rows_snapshot(
                session,
                ReviewDecision,
                [column.name for column in ReviewDecision.__table__.columns],
            ),
        }


def _rows_snapshot(session, model, columns: list[str], *, where=None):
    statement = select(model)
    if where is not None:
        statement = statement.where(where)
    statement = statement.order_by(model.id)
    return tuple(
        tuple(getattr(row, column) for column in columns)
        for row in session.scalars(statement).all()
    )


def test_context_history_repair_restores_a_live_staged_draft(tmp_path: Path):
    """A broken draft pointer must be restored, never silently cleared.

    The published head cannot prove anything about the draft pointer, so
    clearing it here would strand a live staged edit behind a clean audit.
    """

    database_url, session_factory, page_id, head_id, draft_id = _coexist_draft_database(
        tmp_path
    )
    runner = CliRunner()
    base_args = ["context", "history", "repair", "--db", database_url]

    dry_run = runner.invoke(cli_main.app, [*base_args, "--dry-run"])
    applied = runner.invoke(cli_main.app, [*base_args, "--apply"])

    assert dry_run.exit_code == 1, dry_run.output
    assert "latest_draft_status_contradiction" in dry_run.stdout
    assert "blocking=0" in dry_run.stdout
    assert applied.exit_code == 0, applied.output

    page_state, versions = _history_snapshot(session_factory, page_id)
    # current/published semantics are untouched and the staged draft is the
    # reachable editable head again, which is exactly what publication
    # requires of `latest_draft_version_id`.
    assert page_state == (head_id, head_id, draft_id)
    assert versions[draft_id] == ("draft", "linear")
    assert versions[head_id] == ("published_snapshot", "linear")

    # Repair is idempotent: a second apply finds nothing left to do.
    second = runner.invoke(cli_main.app, [*base_args, "--apply"])
    assert second.exit_code == 0, second.output
    assert _history_snapshot(session_factory, page_id) == (page_state, versions)


def test_context_history_repair_preserves_a_draft_under_active_review(
    tmp_path: Path,
):
    """An active review corroborates the draft; repair must not strand it."""

    database_url, session_factory, page_id, head_id, draft_id = _coexist_draft_database(
        tmp_path, review_status="changes_requested"
    )
    before_reviews = _review_snapshot(session_factory)

    applied = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", database_url],
    )

    assert applied.exit_code == 0, applied.output
    page_state, versions = _history_snapshot(session_factory, page_id)
    assert page_state == (head_id, head_id, draft_id)
    assert versions[draft_id] == ("draft", "linear")
    # The review still speaks for a version the page can still reach.
    assert _review_snapshot(session_factory) == before_reviews


def test_context_history_repair_refuses_ambiguous_draft_candidates(
    tmp_path: Path,
):
    """Two linear drafts and no current-pointer proof must abort, not guess."""

    database_url, session_factory, page_id, _head_id, _draft_id = (
        _coexist_draft_database(tmp_path, second_draft=True)
    )
    before = _history_snapshot(session_factory, page_id)
    runner = CliRunner()
    base_args = ["context", "history", "repair", "--db", database_url]

    dry_run = runner.invoke(cli_main.app, [*base_args, "--dry-run"])
    applied = runner.invoke(cli_main.app, [*base_args, "--apply"])

    assert dry_run.exit_code == 1, dry_run.output
    assert "latest_draft_status_contradiction" in dry_run.stdout
    assert "blocking=1" in dry_run.stdout
    assert applied.exit_code == 2
    assert "apply-refused" in applied.stdout
    assert "no changes were applied" in applied.stderr
    assert _history_snapshot(session_factory, page_id) == before


def _review_snapshot(session_factory):
    with session_factory() as session:
        return _rows_snapshot(
            session,
            ReviewItem,
            [column.name for column in ReviewItem.__table__.columns],
        )


def _coexist_draft_database(
    tmp_path: Path,
    *,
    second_draft: bool = False,
    review_status: str | None = None,
):
    """The supported coexist shape with a corrupted ``latest_draft`` pointer.

    ``current_version_id`` names the published head while
    ``latest_draft_version_id`` names a live linear draft -- the shape autosave
    staging produces. The pointer is corrupted to a same-page superseded row
    rather than a random UUID because an enforced foreign key rejects the
    latter outright.
    """

    suffix = "ambiguous" if second_draft else (review_status or "coexist")
    database_url, session_factory, workspace_id = _empty_database(
        tmp_path / f"coexist-{suffix}.db"
    )
    with session_factory.begin() as session:
        page = _page(workspace_id, slug=f"coexist-{suffix}")
        session.add(page)
        session.flush()
        old = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=1,
            status="superseded_snapshot",
            graph_status="linear",
        )
        session.add(old)
        session.flush()
        head = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=2,
            status="published_snapshot",
            graph_status="linear",
            parent_version_id=old.id,
        )
        session.add(head)
        session.flush()
        draft = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=3,
            status="draft",
            graph_status="linear",
            parent_version_id=head.id,
        )
        session.add(draft)
        session.flush()
        if second_draft:
            session.add(
                _version(
                    page.id,
                    workspace_id=workspace_id,
                    version_no=4,
                    status="draft",
                    graph_status="linear",
                    parent_version_id=head.id,
                )
            )
            session.flush()
        if review_status is not None:
            change_set = ChangeSet(
                workspace_id=workspace_id,
                reason="Staged draft awaiting review",
                trigger_type="test_seed",
                actor_type="human",
                actor_id=UUID(ACTOR_ID),
                status="pending",
                risk_level="low",
            )
            session.add(change_set)
            session.flush()
            draft.change_set_id = change_set.id
            session.add(
                ReviewItem(
                    workspace_id=workspace_id,
                    change_set_id=change_set.id,
                    review_type="context_update",
                    status=review_status,
                    risk_level="low",
                    decision_summary="Draft under active review",
                )
            )
            session.flush()
        page.current_version_id = head.id
        page.current_published_version_id = head.id
        page.lifecycle_status = "published"
        # The defect under test: the draft pointer no longer names the draft.
        page.latest_draft_version_id = old.id
        ids = (page.id, head.id, draft.id)
    return database_url, session_factory, *ids


def test_context_history_repair_refuses_overlap_that_masks_live_drafts(
    tmp_path: Path,
):
    """`latest_draft == published head` is not a free win.

    Clearing that overlap is only deterministic while it hides nothing. With
    two live linear drafts on the page the pointer is ambiguous, so the audit
    must call it blocking and `--apply` must change nothing at all.
    """

    database_url, session_factory, page_id, _head_id, _draft_id = _overlap_database(
        tmp_path, draft_count=2
    )
    before = _history_snapshot(session_factory, page_id)
    runner = CliRunner()
    base_args = ["context", "history", "repair", "--db", database_url]

    dry_run = runner.invoke(cli_main.app, [*base_args, "--dry-run"])
    applied = runner.invoke(cli_main.app, [*base_args, "--apply"])

    assert dry_run.exit_code == 1, dry_run.output
    assert "published_head_marked_as_draft_head" in dry_run.stdout
    assert "blocking=1" in dry_run.stdout
    assert applied.exit_code == 2
    assert "apply-refused" in applied.stdout
    assert "no changes were applied" in applied.stderr
    # Exact no-mutation: pointers and every version row are byte-identical.
    assert _history_snapshot(session_factory, page_id) == before


def test_context_history_repair_clears_overlap_when_it_masks_nothing(
    tmp_path: Path,
):
    """With no live draft behind it, the redundant overlap still clears."""

    database_url, session_factory, page_id, head_id, _draft_id = _overlap_database(
        tmp_path, draft_count=0
    )

    applied = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", database_url],
    )

    assert applied.exit_code == 0, applied.output
    page_state, _versions = _history_snapshot(session_factory, page_id)
    assert page_state == (head_id, head_id, None)


def test_context_history_repair_restores_the_sole_draft_behind_an_overlap(
    tmp_path: Path,
):
    """One live draft is proof, so the overlap resolves to that draft."""

    database_url, session_factory, page_id, head_id, draft_id = _overlap_database(
        tmp_path, draft_count=1
    )

    applied = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", database_url],
    )

    assert applied.exit_code == 0, applied.output
    page_state, versions = _history_snapshot(session_factory, page_id)
    assert page_state == (head_id, head_id, draft_id)
    assert versions[draft_id] == ("draft", "linear")


def test_context_history_repair_restores_both_corrupt_pointers_from_one_draft(
    tmp_path: Path,
):
    """A unique draft alone proves the current pointer, with no published head.

    This page was never published, so `proven_published_head` is None and the
    corrupt `latest_draft` pointer is no help either. The sole linear draft is
    the only remaining proof, and without it the current pointer would be
    classified blocking and the whole repair refused.
    """

    database_url, session_factory, page_id, draft_id = _unpublished_draft_database(
        tmp_path
    )

    dry_run = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--dry-run", "--db", database_url],
    )
    applied = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", database_url],
    )

    assert dry_run.exit_code == 1, dry_run.output
    assert "current_version_status_contradiction" in dry_run.stdout
    assert "blocking=0" in dry_run.stdout
    assert applied.exit_code == 0, applied.output
    page_state, versions = _history_snapshot(session_factory, page_id)
    # Both edit-head pointers resolve to the proven draft; nothing is published.
    assert page_state == (draft_id, None, draft_id)
    assert versions[draft_id] == ("draft", "linear")


def _unpublished_draft_database(tmp_path: Path):
    """A never-published page whose two edit-head pointers are both corrupt."""

    database_url, session_factory, workspace_id = _empty_database(
        tmp_path / "unpublished-draft.db"
    )
    with session_factory.begin() as session:
        page = _page(workspace_id, slug="unpublished-draft")
        session.add(page)
        session.flush()
        stale = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=1,
            status="superseded_snapshot",
            graph_status="linear",
        )
        session.add(stale)
        session.flush()
        draft = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=2,
            status="draft",
            graph_status="linear",
            parent_version_id=stale.id,
        )
        session.add(draft)
        session.flush()
        # No published snapshot exists, so no published head can prove anything.
        page.current_published_version_id = None
        page.current_version_id = stale.id
        page.latest_draft_version_id = stale.id
        ids = (page.id, draft.id)
    return database_url, session_factory, *ids


def _overlap_database(
    tmp_path: Path,
    *,
    draft_count: int,
    corrupt_current: bool = False,
):
    """A page whose ``latest_draft`` redundantly names its published head.

    ``draft_count`` controls what that overlap is hiding: nothing, one provable
    draft, or two ambiguous ones. ``corrupt_current`` additionally points the
    current pointer at a superseded snapshot so both edit-head pointers are
    broken at once.
    """

    name = f"overlap-{draft_count}{'-current' if corrupt_current else ''}"
    database_url, session_factory, workspace_id = _empty_database(
        tmp_path / f"{name}.db"
    )
    with session_factory.begin() as session:
        page = _page(workspace_id, slug=name)
        session.add(page)
        session.flush()
        old = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=1,
            status="superseded_snapshot",
            graph_status="linear",
        )
        session.add(old)
        session.flush()
        head = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=2,
            status="published_snapshot",
            graph_status="linear",
            parent_version_id=old.id,
        )
        session.add(head)
        session.flush()
        draft_ids = []
        for index in range(draft_count):
            draft = _version(
                page.id,
                workspace_id=workspace_id,
                version_no=3 + index,
                status="draft",
                graph_status="linear",
                parent_version_id=head.id,
            )
            session.add(draft)
            session.flush()
            draft_ids.append(draft.id)
        page.current_version_id = old.id if corrupt_current else head.id
        page.current_published_version_id = head.id
        page.lifecycle_status = "published"
        # The defect under test: the draft pointer names the published head.
        page.latest_draft_version_id = head.id
        ids = (page.id, head.id, draft_ids[0] if draft_ids else None)
    return database_url, session_factory, *ids


def test_migration_preflight_blocks_overlap_that_masks_a_live_draft(tmp_path: Path):
    """The migration must not clear an overlap that is hiding a live draft.

    `_normalize_context_history` clears `latest_draft == published head`
    unconditionally, so the preflight has to stop the upgrade while that
    overlap is the only reference to a linear draft. A unique draft is
    repairable by the documented command, after which the upgrade proceeds;
    genuine ambiguity stays blocked in both tools.
    """

    migration = import_module(
        "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.workspace_schema"
    )
    unique_url, unique_factory, _page_id, _head_id, _draft_id = _overlap_database(
        tmp_path, draft_count=1
    )
    ambiguous_url, ambiguous_factory, *_rest = _overlap_database(
        tmp_path, draft_count=2
    )

    def counts(session_factory):
        with session_factory.kw["bind"].connect() as connection:
            return migration._context_history_preflight_counts(connection)

    def preflight_error(session_factory) -> str | None:
        with session_factory.kw["bind"].connect() as connection:
            try:
                migration._assert_context_history_preflight(connection)
            except RuntimeError as exc:
                return str(exc)
        return None

    # Both shapes are blockers before any repair.
    assert counts(unique_factory)["latest_draft_overlap_masks_live_draft"] == 1
    assert counts(ambiguous_factory)["latest_draft_overlap_masks_live_draft"] == 1
    assert "latest_draft_overlap_masks_live_draft=1" in (
        preflight_error(unique_factory) or ""
    )

    runner = CliRunner()
    repaired = runner.invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", unique_url],
    )
    refused = runner.invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", ambiguous_url],
    )

    # A proven draft is restored, the overlap is gone, and the upgrade may run.
    assert repaired.exit_code == 0, repaired.output
    assert counts(unique_factory)["latest_draft_overlap_masks_live_draft"] == 0
    assert preflight_error(unique_factory) is None
    # Ambiguity is refused by the CLI and still blocks the upgrade.
    assert refused.exit_code == 2
    assert counts(ambiguous_factory)["latest_draft_overlap_masks_live_draft"] == 1
    assert "latest_draft_overlap_masks_live_draft=1" in (
        preflight_error(ambiguous_factory) or ""
    )


def test_context_history_repair_restores_an_unset_latest_from_the_sole_draft(
    tmp_path: Path,
):
    """An unset draft pointer contradicts nothing, so nothing used to flag it."""

    database_url, session_factory, page_id, head_id, draft_id = _unset_pointer_database(
        tmp_path, draft_count=1
    )
    before_evidence = _preserved_history_evidence_snapshot(session_factory, page_id)
    runner = CliRunner()
    base_args = ["context", "history", "repair", "--db", database_url]

    dry_run = runner.invoke(cli_main.app, [*base_args, "--dry-run"])
    applied = runner.invoke(cli_main.app, [*base_args, "--apply"])

    assert dry_run.exit_code == 1, dry_run.output
    assert "latest_draft_unset_with_live_draft" in dry_run.stdout
    assert "blocking=0" in dry_run.stdout
    assert applied.exit_code == 0, applied.output
    page_state, versions = _history_snapshot(session_factory, page_id)
    # Only the unset pointer moves: the current pointer already named the
    # published head validly, and repairing one pointer must not relocate the
    # other.
    assert page_state == (head_id, head_id, draft_id)
    assert versions[draft_id] == ("draft", "linear")
    _assert_history_evidence_preserved(session_factory, page_id, before_evidence)

    second = runner.invoke(cli_main.app, [*base_args, "--apply"])
    assert second.exit_code == 0, second.output
    assert "pages=0 versions=0" in second.stdout
    assert _history_snapshot(session_factory, page_id) == (page_state, versions)


def test_context_history_repair_restores_unset_current_on_a_published_only_page(
    tmp_path: Path,
):
    """With zero live drafts the proven published head is the only answer."""

    database_url, session_factory, page_id, head_id, _draft_id = (
        _unset_pointer_database(tmp_path, draft_count=0, unset_current=True)
    )

    dry_run = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--dry-run", "--db", database_url],
    )
    applied = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", database_url],
    )

    assert dry_run.exit_code == 1, dry_run.output
    assert "current_version_unset" in dry_run.stdout
    assert "latest_draft_unset_with_live_draft" not in dry_run.stdout
    assert "blocking=0" in dry_run.stdout
    assert applied.exit_code == 0, applied.output
    page_state, _versions = _history_snapshot(session_factory, page_id)
    assert page_state == (head_id, head_id, None)


def test_context_history_repair_restores_both_unset_pointers_from_one_draft(
    tmp_path: Path,
):
    """Both pointers unset: the draft outranks the published head."""

    database_url, session_factory, page_id, head_id, draft_id = _unset_pointer_database(
        tmp_path, draft_count=1, unset_current=True
    )

    dry_run = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--dry-run", "--db", database_url],
    )
    applied = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", database_url],
    )

    assert dry_run.exit_code == 1, dry_run.output
    assert "current_version_unset" in dry_run.stdout
    assert "latest_draft_unset_with_live_draft" in dry_run.stdout
    assert "blocking=0" in dry_run.stdout
    assert applied.exit_code == 0, applied.output
    page_state, versions = _history_snapshot(session_factory, page_id)
    assert page_state == (draft_id, head_id, draft_id)
    assert versions[head_id] == ("published_snapshot", "linear")


def test_context_history_repair_uses_the_current_pointer_to_prove_unset_latest(
    tmp_path: Path,
):
    """Two drafts are ambiguous alone; a valid current pointer resolves them."""

    database_url, session_factory, page_id, head_id, draft_id = _unset_pointer_database(
        tmp_path, draft_count=2, current_names_draft=True
    )

    applied = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--apply", "--db", database_url],
    )

    assert applied.exit_code == 0, applied.output
    page_state, _versions = _history_snapshot(session_factory, page_id)
    assert page_state == (draft_id, head_id, draft_id)


def test_context_history_repair_refuses_multiple_unpointed_drafts(tmp_path: Path):
    """Ambiguity aborts atomically: exact no-mutation, including every row field."""

    database_url, session_factory, page_id, _head_id, _draft_id = (
        _unset_pointer_database(tmp_path, draft_count=2, unset_current=True)
    )
    before_state = _history_snapshot(session_factory, page_id)
    before_evidence = _preserved_history_evidence_snapshot(session_factory, page_id)
    runner = CliRunner()
    base_args = ["context", "history", "repair", "--db", database_url]

    dry_run = runner.invoke(cli_main.app, [*base_args, "--dry-run"])
    applied = runner.invoke(cli_main.app, [*base_args, "--apply"])

    assert dry_run.exit_code == 1, dry_run.output
    assert "current_version_unset" in dry_run.stdout
    assert "latest_draft_unset_with_live_draft" in dry_run.stdout
    assert "blocking=2" in dry_run.stdout
    assert applied.exit_code == 2
    assert "apply-refused" in applied.stdout
    assert "no changes were applied" in applied.stderr
    assert _history_snapshot(session_factory, page_id) == before_state
    _assert_history_evidence_preserved(session_factory, page_id, before_evidence)


def test_context_history_audit_accepts_a_page_with_no_versions(tmp_path: Path):
    """An empty page has no edit head to be missing; it must stay clean."""

    database_url, session_factory, workspace_id = _empty_database(
        tmp_path / "empty-page.db"
    )
    with session_factory.begin() as session:
        session.add(_page(workspace_id, slug="never-written"))

    dry_run = CliRunner().invoke(
        cli_main.app,
        ["context", "history", "repair", "--dry-run", "--db", database_url],
    )

    assert dry_run.exit_code == 0, dry_run.output
    assert "violations=0" in dry_run.stdout


def test_unset_pointer_audit_and_migration_preflight_agree(tmp_path: Path):
    """The audit class and the preflight blocker must describe one state.

    Equivalence in both directions: while the audit reports an unset-pointer
    violation the upgrade is blocked, and once the repair clears the audit the
    preflight count returns to zero. Ambiguity stays blocked in both tools.
    """

    migration = import_module(
        "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.workspace_schema"
    )
    unique_url, unique_factory, *_unique_ids = _unset_pointer_database(
        tmp_path, draft_count=1, unset_current=True
    )
    ambiguous_url, ambiguous_factory, *_other_ids = _unset_pointer_database(
        tmp_path, draft_count=2, unset_current=True
    )

    def counts(session_factory):
        with session_factory.kw["bind"].connect() as connection:
            return migration._context_history_preflight_counts(connection)

    def audit_codes(session_factory):
        with session_factory() as session:
            return {
                violation.code
                for violation in audit_context_history(session).violations
            }

    for factory in (unique_factory, ambiguous_factory):
        assert counts(factory)["current_version_unset_with_versions"] == 1
        assert counts(factory)["latest_draft_unset_with_live_draft"] == 1
        codes = audit_codes(factory)
        assert "current_version_unset" in codes
        assert "latest_draft_unset_with_live_draft" in codes

    runner = CliRunner()
    repaired = runner.invoke(
        cli_main.app, ["context", "history", "repair", "--apply", "--db", unique_url]
    )
    refused = runner.invoke(
        cli_main.app, ["context", "history", "repair", "--apply", "--db", ambiguous_url]
    )

    # A uniquely proven draft clears both the audit and every preflight count.
    assert repaired.exit_code == 0, repaired.output
    assert not any(counts(unique_factory).values())
    assert audit_codes(unique_factory) == set()
    # Ambiguity is refused by the CLI and still blocks the upgrade.
    assert refused.exit_code == 2
    assert counts(ambiguous_factory)["current_version_unset_with_versions"] == 1
    assert counts(ambiguous_factory)["latest_draft_unset_with_live_draft"] == 1


def _assert_history_evidence_preserved(session_factory, page_id, before):
    """Bodies, hashes, version numbers, reviews and change sets are untouched."""

    after = _preserved_history_evidence_snapshot(session_factory, page_id)
    assert (
        after["version_bodies_hashes_numbers"]
        == (before["version_bodies_hashes_numbers"])
    )
    assert after["version_non_status_fields"] == before["version_non_status_fields"]
    assert after["page_non_pointer_fields"] == before["page_non_pointer_fields"]
    assert after["citations"] == before["citations"]
    assert after["change_sets"] == before["change_sets"]
    assert after["review_decisions"] == before["review_decisions"]


def _unset_pointer_database(
    tmp_path: Path,
    *,
    draft_count: int,
    unset_current: bool = False,
    current_names_draft: bool = False,
):
    """A published page whose edit-head pointers were never written.

    ``latest_draft_version_id`` is always NULL here, which is the defect under
    test. ``draft_count`` controls what that unset pointer is hiding, and
    ``unset_current`` additionally clears the current pointer so both edit
    heads are unset at once.
    """

    name = (
        f"unset-{draft_count}"
        f"{'-both' if unset_current else ''}"
        f"{'-currentproof' if current_names_draft else ''}"
    )
    database_url, session_factory, workspace_id = _empty_database(
        tmp_path / f"{name}.db"
    )
    with session_factory.begin() as session:
        page = _page(workspace_id, slug=name)
        session.add(page)
        session.flush()
        head = _version(
            page.id,
            workspace_id=workspace_id,
            version_no=1,
            status="published_snapshot",
            graph_status="linear",
        )
        session.add(head)
        session.flush()
        draft_ids = []
        for index in range(draft_count):
            draft = _version(
                page.id,
                workspace_id=workspace_id,
                version_no=2 + index,
                status="draft",
                graph_status="linear",
                parent_version_id=head.id,
            )
            session.add(draft)
            session.flush()
            draft_ids.append(draft.id)
        page.current_published_version_id = head.id
        page.lifecycle_status = "published"
        if current_names_draft:
            page.current_version_id = draft_ids[0]
        elif unset_current:
            page.current_version_id = None
        else:
            page.current_version_id = head.id
        # The defect under test: the draft pointer was never written.
        page.latest_draft_version_id = None
        ids = (page.id, head.id, draft_ids[0] if draft_ids else None)
    return database_url, session_factory, *ids
