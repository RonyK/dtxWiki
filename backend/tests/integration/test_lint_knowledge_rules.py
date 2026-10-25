from uuid import uuid4

import pytest
from sqlalchemy import event, select

from backend.models.context import ContextPage, ContextPageVersion, PageNameIndex
from backend.models.lint import LintFinding
from backend.models.workspace import Workspace
from backend.schemas.lint import LintRunCreateRequest
from backend.services.lint import LintContext, LintRegistry
from backend.services.lint.scopes import findings_for_target
from backend.services.lint_service import LintService
from backend.services.page_service import refresh_page_links

pytestmark = pytest.mark.integration


def _ws(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _page(session, wsid, slug, body="", title=None):
    page = ContextPage(
        workspace_id=wsid, slug=slug, title=title or slug, page_type="concept"
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=page.workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=body,
        content_hash="h" + uuid4().hex[:8],
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="t",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_published_version_id = version.id
    page.current_version_id = version.id
    session.flush()
    return page


def _refresh(session, *pages):
    for page in pages:
        version = session.get(ContextPageVersion, page.current_version_id)
        refresh_page_links(session, page=page, version=version)


def _check(session, rule_id, page):
    return findings_for_target(
        LintRegistry.builtin().rule(rule_id),
        page,
        LintContext(session, page.workspace_id, page=page),
    )


def test_orphan_page(session_factory):
    with session_factory.begin() as session:
        wsid = _ws(session)
        hub = _page(session, wsid, "hub", body="See [[leaf]].")
        leaf = _page(session, wsid, "leaf")
        _refresh(session, hub, leaf)
        # leaf has an inbound link from hub -> not an orphan.
        assert _check(session, "kb.orphan_page", leaf) == []
        # hub has no inbound link -> orphan.
        findings = _check(session, "kb.orphan_page", hub)
    assert len(findings) == 1
    assert findings[0].rule_id == "kb.orphan_page"
    assert findings[0].severity == "info"


def test_missing_backlink_flagged(session_factory):
    with session_factory.begin() as session:
        wsid = _ws(session)
        a = _page(session, wsid, "a", body="[[b]]")
        b = _page(session, wsid, "b")
        _refresh(session, a, b)
        findings = _check(session, "kb.missing_backlink", a)
    assert len(findings) == 1
    assert "does not link back" in findings[0].suggested_action


def test_mutual_backlink_clean(session_factory):
    with session_factory.begin() as session:
        wsid = _ws(session)
        a = _page(session, wsid, "ma", body="[[mb]]")
        b = _page(session, wsid, "mb", body="[[ma]]")
        _refresh(session, a, b)
        findings = _check(session, "kb.missing_backlink", a)
    assert findings == []


def test_duplicate_concept_flagged(session_factory):
    with session_factory.begin() as session:
        wsid = _ws(session)
        p1 = _page(session, wsid, "dc1", title="Payment Guide")
        _page(session, wsid, "dc2", title="payment guide")
        findings = _check(session, "kb.duplicate_concept", p1)
    assert len(findings) == 1
    assert findings[0].severity == "warning"


def test_unique_title_clean(session_factory):
    with session_factory.begin() as session:
        wsid = _ws(session)
        p = _page(session, wsid, "uq", title="Unique Title XYZ")
        findings = _check(session, "kb.duplicate_concept", p)
    assert findings == []


def test_knowledge_rules_consume_current_page_link_projection(session_factory):
    with session_factory.begin() as session:
        wsid = _ws(session)
        source = _page(session, wsid, "src", body="See [[real]].")
        target = _page(session, wsid, "real")
        _refresh(session, source, target)

        findings = _check(session, "kb.missing_backlink", source)

    assert len(findings) == 1
    assert findings[0].fingerprint.endswith("|real")


def test_knowledge_rules_remain_published_scoped_when_draft_links_change(
    session_factory,
):
    with session_factory.begin() as session:
        wsid = _ws(session)
        source = _page(
            session,
            wsid,
            "published-source",
            body="See [[published-target]].",
        )
        target = _page(session, wsid, "published-target")
        _refresh(session, source, target)
        published_version_id = source.current_published_version_id
        draft = ContextPageVersion(
            workspace_id=source.workspace_id,
            page_id=source.id,
            parent_version_id=published_version_id,
            version_no=2,
            body_md="Draft removes the published link.",
            content_hash="draft-" + uuid4().hex,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="t",
            version_status="draft",
        )
        session.add(draft)
        session.flush()
        source.current_version_id = draft.id
        source.latest_draft_version_id = draft.id
        refresh_page_links(session, page=source, version=draft)

        assert _check(session, "kb.orphan_page", target) == []
        missing = _check(session, "kb.missing_backlink", source)
        assert len(missing) == 1
        assert missing[0].fingerprint.endswith("|published-target")


def test_knowledge_rules_ignore_draft_only_pages(session_factory):
    with session_factory.begin() as session:
        wsid = _ws(session)
        draft_only = ContextPage(
            workspace_id=wsid,
            slug="draft-only-lint",
            title="Duplicate Draft Name",
            page_type="concept",
            lifecycle_status="draft",
        )
        published = _page(session, wsid, "published-name", title="Duplicate Draft Name")
        session.add(draft_only)
        session.flush()

        assert _check(session, "kb.orphan_page", draft_only) == []
        assert _check(session, "kb.missing_backlink", draft_only) == []
        assert _check(session, "kb.duplicate_concept", draft_only) == []
        assert _check(session, "kb.duplicate_concept", published) == []


def test_duplicate_concept_ignores_archived(session_factory):
    # M8: an archived same-title page is not a live duplicate.
    with session_factory.begin() as session:
        wsid = _ws(session)
        live = _page(session, wsid, "dc-live", title="Payment Guide")
        old = _page(session, wsid, "dc-old", title="Payment Guide")
        old.lifecycle_status = "archived"
        session.flush()
        assert _check(session, "kb.duplicate_concept", live) == []


def test_run_lint_clears_rule_caches_between_runs(session_factory):
    svc = LintService()
    policy = {"kb.orphan_page": True}
    req = LintRunCreateRequest(lint_scope="workspace", trigger_type="manual")
    with session_factory.begin() as session:
        wsid = _ws(session)
        session.info["_lint_nav_reachable"] = {"stale": {uuid4()}}
        session.info["_lint_term_collision_map"] = {uuid4(): ["stale"]}
        lonely = _page(session, wsid, "lonely")
        svc.run_lint(session, wsid, req, "human", "t", policy=policy)
        assert "stale" not in session.info.get("_lint_nav_reachable", {})
        assert all(
            "stale" not in names
            for names in session.info.get("_lint_term_collision_map", {}).values()
        )
        open1 = session.scalars(
            select(LintFinding).where(
                LintFinding.rule_id == "kb.orphan_page",
                LintFinding.target_id == lonely.id,
                LintFinding.status == "open",
            )
        ).all()
        assert len(open1) == 1
        # add an inbound link in the SAME session, then re-run
        hub = _page(session, wsid, "hub", body="See [[lonely]].")
        _refresh(session, hub, lonely)
        svc.run_lint(session, wsid, req, "human", "t", policy=policy)
        still_open = session.scalars(
            select(LintFinding).where(
                LintFinding.rule_id == "kb.orphan_page",
                LintFinding.target_id == lonely.id,
                LintFinding.status == "open",
            )
        ).all()
    assert still_open == []


def test_knowledge_rules_are_default_on():
    # Sprint-6 decision: knowledge checks apply to existing and new workspaces.
    ids = {
        r.rule_id
        for r in LintRegistry.builtin().enabled(None, target_type="context_page")
    }
    assert "kb.orphan_page" in ids
    assert "kb.missing_backlink" in ids
    assert "kb.duplicate_concept" in ids
    # Workspace policy can still explicitly disable a noisy rule.
    off = {
        r.rule_id
        for r in LintRegistry.builtin().enabled(
            {"kb.orphan_page": False}, target_type="context_page"
        )
    }
    assert "kb.orphan_page" not in off


def test_term_collision_detects_cross_page_name_projection(session_factory):
    with session_factory.begin() as session:
        wsid = _ws(session)
        first = _page(session, wsid, "collision-a", title="Shared Name")
        second = _page(session, wsid, "collision-b", title="Other")
        session.add_all(
            [
                PageNameIndex(
                    workspace_id=wsid,
                    page_id=first.id,
                    source_version_id=first.current_version_id,
                    visibility_scope="published",
                    name_norm="shared name",
                    name="Shared Name",
                    name_kind="title",
                ),
                PageNameIndex(
                    workspace_id=wsid,
                    page_id=second.id,
                    source_version_id=second.current_version_id,
                    visibility_scope="published",
                    name_norm="shared name",
                    name="Shared Name",
                    name_kind="alias",
                ),
            ]
        )
        session.flush()

        findings = _check(session, "kb.term_collision", first)

    assert len(findings) == 1
    assert findings[0].fingerprint == "kb.term_collision|shared name"


def test_term_collision_projection_is_loaded_once_per_run(session_factory):
    with session_factory.begin() as session:
        wsid = _ws(session)
        first = _page(session, wsid, "memo-a", title="Memo Shared")
        second = _page(session, wsid, "memo-b", title="Memo Other")
        session.add_all(
            [
                PageNameIndex(
                    workspace_id=wsid,
                    page_id=first.id,
                    source_version_id=first.current_published_version_id,
                    visibility_scope="published",
                    name_norm="memo shared",
                    name="Memo Shared",
                    name_kind="title",
                ),
                PageNameIndex(
                    workspace_id=wsid,
                    page_id=second.id,
                    source_version_id=second.current_published_version_id,
                    visibility_scope="published",
                    name_norm="memo shared",
                    name="Memo Shared",
                    name_kind="alias",
                ),
            ]
        )
        session.flush()
        collision_selects = 0

        def count_collision_query(_conn, _cursor, statement, *_args):
            nonlocal collision_selects
            if (
                statement.lstrip().upper().startswith("SELECT")
                and "page_name_index" in statement
            ):
                collision_selects += 1

        event.listen(session.get_bind(), "before_cursor_execute", count_collision_query)
        try:
            assert _check(session, "kb.term_collision", first)
            assert _check(session, "kb.term_collision", second)
        finally:
            event.remove(
                session.get_bind(), "before_cursor_execute", count_collision_query
            )

    assert collision_selects == 1
