from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models import User, WorkspaceMembership
from backend.models.context import ContextPage
from backend.models.lint import LintFinding, LintRun
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.rest_scopes import require_token_scope
from backend.schemas.lint import (
    LintFindingBatchDecisionRequest,
    LintFindingDecisionRequest,
    LintRunCreateRequest,
)
from backend.services.lint import LintContext, LintModule, LintRegistry, LintStage
from backend.services.lint.base import LintFindingDraft
from backend.services.lint.scopes import findings_for_target
from backend.services.lint_service import LintService
from backend.services.read_errors import ReadValidationError
from backend.tests.conftest import authenticate_test_client

pytestmark = pytest.mark.integration


class _Dummy:
    stages = frozenset({LintStage.RUN})
    rule_id = "test.dummy_format"
    layer = "format"
    default_enabled = True
    applies_to = frozenset({"context_page"})

    def check_target(self, target, ctx):
        return [
            LintFindingDraft(
                rule_id=self.rule_id,
                severity="warning",
                target_type="context_page",
                target_id=target.id,
                suggested_action="dummy hit",
            )
        ]


def _ws(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _page(session, wsid, slug="lintp1"):
    page = ContextPage(workspace_id=wsid, slug=slug, title="P1", page_type="concept")
    session.add(page)
    session.flush()
    return page


def test_list_findings_hides_draft_only_page_from_viewer(session_factory):
    # M-02: a finding on a draft-only Context page (which 404s on direct GET for
    # a viewer) must be excluded from the viewer's lint list AND its total, while
    # an owner still sees it.
    from backend.models.context import ContextPageVersion
    from backend.policy.auth import ActorContext

    with session_factory.begin() as session:
        wsid = _ws(session)
        run = LintRun(
            workspace_id=wsid,
            lint_scope={"kind": "maintenance"},
            trigger_type="wiki_maintenance",
            status="completed",
            finding_count_by_severity={"info": 1},
        )
        session.add(run)
        session.flush()
        draft = ContextPage(
            workspace_id=wsid,
            slug="m02-draft-lint",
            title="M02 Draft Lint Target",
            page_type="concept",
            lifecycle_status="draft",
        )
        session.add(draft)
        session.flush()
        version = ContextPageVersion(
            workspace_id=wsid,
            page_id=draft.id,
            version_no=1,
            body_md="# M02\n\nDraft.",
            content_hash="m02-draft-lint-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        session.add(version)
        session.flush()
        draft.current_version_id = version.id
        draft.latest_draft_version_id = version.id
        finding = LintFinding(
            lint_run_id=run.id,
            workspace_id=wsid,
            target_type="context_page",
            target_id=draft.id,
            rule_id="knowledge.missing_backlink",
            severity="info",
            status="open",
            fingerprint="m02:draft",
        )
        session.add(finding)
        session.flush()

        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer-user",
            token_kind="session",
            scopes=("read",),
            workspace_id=wsid,
            workspace_role="viewer",
            role="viewer",
        )
        owner = ActorContext(
            actor_kind="system",
            actor_id="owner",
            scopes=("*",),
            workspace_id=wsid,
            workspace_role="owner",
            role="owner",
        )
        viewer_list = LintService().list_findings(session, wsid, actor=viewer)
        owner_list = LintService().list_findings(session, wsid, actor=owner)

    assert finding.id not in {item.id for item in viewer_list.findings}
    assert viewer_list.total == 0
    assert finding.id in {item.id for item in owner_list.findings}


def test_sensitive_agent_tree_node_finding_hidden_from_editor(session_factory):
    # M-02 (Codex round-2): a lint finding on a sensitive AgentPage tree node is
    # excluded from an editor's list and its decide endpoint 404s, while an owner
    # sees it.
    from backend.models.agent import AgentPage, AgentPageVersion
    from backend.policy.auth import ActorContext
    from backend.schemas.lint import LintFindingDecisionRequest
    from backend.services.read_errors import ReadNotFoundError
    from backend.services.tree_service import ensure_primary_tree_placement

    svc = LintService()
    with session_factory.begin() as session:
        wsid = _ws(session)
        run = LintRun(
            workspace_id=wsid,
            lint_scope={"kind": "maintenance"},
            trigger_type="wiki_maintenance",
            status="completed",
            finding_count_by_severity={"info": 1},
        )
        session.add(run)
        session.flush()
        agent = AgentPage(
            workspace_id=wsid,
            category="persona",
            title="M02 Sensitive Persona",
            slug="m02-sensitive-persona",
            sensitivity="private",
            lifecycle_status="active",
        )
        session.add(agent)
        session.flush()
        version = AgentPageVersion(
            workspace_id=wsid,
            agent_page_id=agent.id,
            version_no=1,
            body_markdown="# Sensitive",
            frontmatter={},
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add(version)
        session.flush()
        agent.current_active_version_id = version.id
        node = ensure_primary_tree_placement(
            session=session,
            workspace_id=wsid,
            target_kind="agent_page",
            target_id=agent.id,
            title=agent.title,
            section="agent",
            parent_id=None,
            default_parent_slug="agent",
            actor_kind="human",
            actor_id="owner",
        )
        finding = LintFinding(
            lint_run_id=run.id,
            workspace_id=wsid,
            target_type="workspace_tree_node",
            target_id=node.id,
            rule_id="maintenance.nav_summary_suggestion",
            severity="info",
            status="open",
            fingerprint="m02:sensitive-node",
        )
        session.add(finding)
        session.flush()
        finding_id = node_id = finding.id

        editor = ActorContext(
            actor_kind="human",
            actor_id="editor-user",
            token_kind="session",
            scopes=("read",),
            workspace_id=wsid,
            workspace_role="editor",
            role="editor",
        )
        owner = ActorContext(
            actor_kind="system",
            actor_id="owner",
            scopes=("*",),
            workspace_id=wsid,
            workspace_role="owner",
            role="owner",
        )
        editor_ids = {
            f.id for f in svc.list_findings(session, wsid, actor=editor).findings
        }
        owner_ids = {
            f.id for f in svc.list_findings(session, wsid, actor=owner).findings
        }
        assert node_id not in editor_ids
        assert finding_id in owner_ids
        with pytest.raises(ReadNotFoundError):
            svc.decide_finding(
                session,
                wsid,
                finding_id,
                LintFindingDecisionRequest(decision="acknowledge"),
                actor_kind="human",
                actor_id="editor-user",
                actor=editor,
            )


def test_list_findings_hides_draft_only_defect_but_keeps_published_one(
    session_factory,
):
    # LEAK-01: a page can have a visible PUBLISHED head and a staged DRAFT at
    # once. The old page-visibility gate only asked "does this page have a
    # published head" -- so a finding produced ONLY by the draft body (here, a
    # heading-hierarchy jump whose suggested_action embeds a confidential
    # title) reached a viewer who cannot read that draft. A defect that ALSO
    # exists in the published head must stay visible to them.
    from backend.models.context import ContextPageVersion
    from backend.policy.auth import ActorContext

    svc = LintService()
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _page(session, wsid, slug="leak01-format-page")
        published = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=1,
            body_md="# Top\n\n### Public Only Jump\n\nPublished body.\n",
            content_hash="leak01-format-pub",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="published_snapshot",
        )
        session.add(published)
        session.flush()
        page.current_published_version_id = published.id
        page.current_version_id = published.id

        # A DIFFERENT jump (different anchor) so it does not collapse with the
        # published-body finding above -- this is the draft-only defect.
        draft = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=2,
            body_md="# Top\n\n##### Acquisition of Northwind\n",
            content_hash="leak01-format-draft",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
            parent_version_id=published.id,
        )
        session.add(draft)
        session.flush()
        page.latest_draft_version_id = draft.id
        page.current_version_id = draft.id
        session.flush()

        svc.run_lint(
            session,
            wsid,
            LintRunCreateRequest(),
            actor_kind="human",
            actor_id="tester",
        )

        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer-user",
            token_kind="session",
            scopes=("read",),
            workspace_id=wsid,
            workspace_role="viewer",
            role="viewer",
        )
        editor = ActorContext(
            actor_kind="human",
            actor_id="editor-user",
            token_kind="session",
            scopes=("read", "write"),
            workspace_id=wsid,
            workspace_role="editor",
            role="editor",
        )

        editor_list = svc.list_findings(
            session, wsid, actor=editor, rule="md.heading_hierarchy"
        )
        viewer_list = svc.list_findings(
            session, wsid, actor=viewer, rule="md.heading_hierarchy"
        )

    editor_findings = {f.id: f for f in editor_list.findings if f.target_id == page.id}
    viewer_findings = {f.id: f for f in viewer_list.findings if f.target_id == page.id}

    # (a) editor (can read drafts) sees BOTH the published-only jump and the
    # draft-only jump, the latter's suggested_action carrying the
    # confidential heading text.
    assert len(editor_findings) == 2
    assert editor_list.total == len(editor_findings)
    assert any(
        "Acquisition of Northwind" in (f.suggested_action or "")
        for f in editor_findings.values()
    )

    # (b) viewer's filtered list -- and its total -- exclude the draft-only
    # finding; the confidential text never reaches them.
    assert len(viewer_findings) == 1
    assert viewer_list.total == 1
    assert not any(
        "Acquisition of Northwind" in (f.suggested_action or "")
        for f in viewer_findings.values()
    )

    # (c) the published-body finding on the SAME page is still visible to the
    # viewer -- proves the fix excludes the one draft-only finding, not the
    # whole page (an over-broad "hide all context_page findings" fix would
    # fail this).
    (remaining,) = viewer_findings.values()
    assert remaining.message_args.get("body_scopes") == ["published"]
    assert "Public Only Jump" in (remaining.suggested_action or "")


def test_list_findings_hides_draft_only_unresolved_link(session_factory):
    # LEAK-01 (rules_links half): an unresolved-wikilink finding must carry
    # the same body-provenance as a format finding, or a viewer who cannot
    # read the draft still sees a slug that only exists in it.
    from backend.models.context import ContextPageVersion
    from backend.policy.auth import ActorContext
    from backend.services.page_service import refresh_page_links

    svc = LintService()
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _page(session, wsid, slug="leak01-link-page")
        published = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=1,
            body_md="No links in the published head.\n",
            content_hash="leak01-link-pub",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="published_snapshot",
        )
        session.add(published)
        session.flush()
        page.current_published_version_id = published.id
        page.current_version_id = published.id
        refresh_page_links(session, page=page, version=published)
        session.flush()

        draft = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=2,
            body_md="See [[secret-slug]] for details.\n",
            content_hash="leak01-link-draft",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
            parent_version_id=published.id,
        )
        session.add(draft)
        session.flush()
        page.latest_draft_version_id = draft.id
        page.current_version_id = draft.id
        refresh_page_links(session, page=page, version=draft)
        session.flush()

        svc.run_lint(
            session,
            wsid,
            LintRunCreateRequest(),
            actor_kind="human",
            actor_id="tester",
        )

        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer-user",
            token_kind="session",
            scopes=("read",),
            workspace_id=wsid,
            workspace_role="viewer",
            role="viewer",
        )
        editor = ActorContext(
            actor_kind="human",
            actor_id="editor-user",
            token_kind="session",
            scopes=("read", "write"),
            workspace_id=wsid,
            workspace_role="editor",
            role="editor",
        )

        editor_list = svc.list_findings(
            session, wsid, actor=editor, rule="link.unresolved"
        )
        viewer_list = svc.list_findings(
            session, wsid, actor=viewer, rule="link.unresolved"
        )

    def _payload_strings(data):
        out = []
        for f in data.findings:
            out.append(f.suggested_action or "")
            out.extend(str(v) for v in (f.message_args or {}).values())
        return out

    assert any("secret-slug" in s for s in _payload_strings(editor_list))
    assert not any("secret-slug" in s for s in _payload_strings(viewer_list))
    assert viewer_list.total == 0


def test_decide_finding_404s_for_draft_only_finding_even_by_direct_id(
    session_factory,
):
    # LEAK-01: a direct finding_id decide (or GET) must not be a bypass of the
    # list_findings exclusion -- a viewer who cannot enumerate a draft-only
    # finding must also not be able to decide it by guessing/reusing its id.
    # The target PAGE is visible (has a published head); only the specific
    # finding is draft-only.
    from backend.models.context import ContextPageVersion
    from backend.policy.auth import ActorContext
    from backend.services.read_errors import ReadNotFoundError

    svc = LintService()
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _page(session, wsid, slug="leak01-decide-page")
        published = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=1,
            body_md="# Top\n\nPublished body, no defects.\n",
            content_hash="leak01-decide-pub",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="published_snapshot",
        )
        session.add(published)
        session.flush()
        page.current_published_version_id = published.id
        page.current_version_id = published.id

        draft = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=2,
            body_md="# Top\n\n### Confidential Draft Jump\n",
            content_hash="leak01-decide-draft",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
            parent_version_id=published.id,
        )
        session.add(draft)
        session.flush()
        page.latest_draft_version_id = draft.id
        page.current_version_id = draft.id
        session.flush()

        svc.run_lint(
            session,
            wsid,
            LintRunCreateRequest(),
            actor_kind="human",
            actor_id="tester",
        )
        finding = session.scalar(
            select(LintFinding).where(
                LintFinding.workspace_id == wsid,
                LintFinding.target_id == page.id,
                LintFinding.rule_id == "md.heading_hierarchy",
            )
        )
        assert finding is not None
        finding_id = finding.id

        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer-user",
            token_kind="session",
            scopes=("read",),
            workspace_id=wsid,
            workspace_role="viewer",
            role="viewer",
        )
        editor = ActorContext(
            actor_kind="human",
            actor_id="editor-user",
            token_kind="session",
            scopes=("read", "write"),
            workspace_id=wsid,
            workspace_role="editor",
            role="editor",
        )

        # The page itself IS visible to the viewer (published head exists) --
        # proves this is not just the pre-existing page-level M-02 gate.
        with pytest.raises(ReadNotFoundError):
            svc.decide_finding(
                session,
                wsid,
                finding_id,
                LintFindingDecisionRequest(decision="acknowledge"),
                actor_kind="human",
                actor_id="viewer-user",
                actor=viewer,
            )
        # A caller that passes actor=None must be refused too, not treated as
        # trusted/unfiltered -- actor_can_read_drafts(None) is False, so
        # _draft_only_hidden falls through to the same hidden-if-draft-only
        # check as the viewer above. decide_finding's `actor` parameter has
        # no default specifically so a caller cannot omit it the way this
        # call site itself used to (and the sibling get_run bug, LEAK-01/
        # LEAK-03 shape, was exactly a missing actor defaulting to "visible").
        with pytest.raises(ReadNotFoundError):
            svc.decide_finding(
                session,
                wsid,
                finding_id,
                LintFindingDecisionRequest(decision="acknowledge"),
                actor_kind="human",
                actor_id="no-actor-caller",
                actor=None,
            )
        # An editor (can read drafts) may decide it normally.
        decided = svc.decide_finding(
            session,
            wsid,
            finding_id,
            LintFindingDecisionRequest(decision="acknowledge"),
            actor_kind="human",
            actor_id="editor-user",
            actor=editor,
        )
        assert decided.status == "acknowledged"


def test_get_run_hides_draft_only_finding_count_from_viewer(session_factory):
    # LEAK-01 (third surface): finding_count_by_severity is a raw tally
    # persisted at run-completion time over EVERY finding the run reproduced,
    # including a finding produced ONLY by a draft body. list_findings hides
    # the ROW, but get_run used to return the unfiltered COUNT to any actor
    # regardless of draft-read access -- a viewer could still learn "there is
    # 1 more finding" for a page whose draft they cannot read. Same page
    # shape as test_list_findings_hides_draft_only_defect_but_keeps_published_one:
    # a published-only heading jump and a SEPARATE draft-only heading jump.
    from backend.models.context import ContextPageVersion
    from backend.policy.auth import ActorContext

    svc = LintService()
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _page(session, wsid, slug="leak01-run-count-page")
        published = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=1,
            body_md="# Top\n\n### Public Only Jump\n\nPublished body.\n",
            content_hash="leak01-run-count-pub",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="published_snapshot",
        )
        session.add(published)
        session.flush()
        page.current_published_version_id = published.id
        page.current_version_id = published.id

        # A DIFFERENT jump (different anchor) so it does not collapse with the
        # published-body finding above -- this is the draft-only defect.
        draft = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=2,
            body_md="# Top\n\n##### Acquisition of Northwind\n",
            content_hash="leak01-run-count-draft",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
            parent_version_id=published.id,
        )
        session.add(draft)
        session.flush()
        page.latest_draft_version_id = draft.id
        page.current_version_id = draft.id
        session.flush()

        run = svc.run_lint(
            session,
            wsid,
            LintRunCreateRequest(),
            actor_kind="human",
            actor_id="tester",
        )

        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer-user",
            token_kind="session",
            scopes=("read",),
            workspace_id=wsid,
            workspace_role="viewer",
            role="viewer",
        )
        editor = ActorContext(
            actor_kind="human",
            actor_id="editor-user",
            token_kind="session",
            scopes=("read", "write"),
            workspace_id=wsid,
            workspace_role="editor",
            role="editor",
        )

        editor_run = svc.get_run(session, wsid, run.id, actor=editor)
        viewer_run = svc.get_run(session, wsid, run.id, actor=viewer)
        # A caller that passes NO actor at all must get the SAFE (filtered)
        # tally, not the unfiltered one -- `actor=None` must fail CLOSED,
        # exactly the shape of bug LEAK-03 fixed elsewhere (a target-metadata
        # projector that treated a missing actor as fully authorized). `actor`
        # has no default specifically so a caller cannot forget to pass it the
        # way the REST route originally did.
        no_actor_run = svc.get_run(session, wsid, run.id, actor=None)

    editor_total = sum(editor_run.finding_count_by_severity.values())
    viewer_total = sum(viewer_run.finding_count_by_severity.values())
    no_actor_total = sum(no_actor_run.finding_count_by_severity.values())

    # (a) the editor (can read drafts) sees the full, unfiltered tally --
    # identical to the persisted historical count.
    assert editor_run.finding_count_by_severity == run.finding_count_by_severity

    # (b) the viewer's tally is missing exactly the one draft-only finding --
    # not the whole page's findings, and not zeroed out either.
    assert viewer_total == editor_total - 1

    # (c) actor=None gets the SAME safe tally as the viewer, not the
    # editor's unfiltered one -- pins the fail-closed contract rather than
    # assuming it.
    assert (
        no_actor_run.finding_count_by_severity == viewer_run.finding_count_by_severity
    )
    assert no_actor_total == editor_total - 1
    assert viewer_run.finding_count_by_severity.get("warning", 0) == (
        editor_run.finding_count_by_severity.get("warning", 0) - 1
    )


def test_lint_finding_on_deleted_target_fails_closed(session_factory):
    # M-02 (Codex round-3): a finding whose Context-page target was deleted must
    # fail CLOSED — its stored suggested_action/message_args hold the old title.
    from uuid import uuid4

    from backend.policy.auth import ActorContext
    from backend.schemas.lint import LintFindingDecisionRequest
    from backend.services.read_errors import ReadNotFoundError

    svc = LintService()
    with session_factory.begin() as session:
        wsid = _ws(session)
        run = LintRun(
            workspace_id=wsid,
            lint_scope={"kind": "maintenance"},
            trigger_type="wiki_maintenance",
            status="completed",
            finding_count_by_severity={"info": 1},
        )
        session.add(run)
        session.flush()
        finding = LintFinding(
            lint_run_id=run.id,
            workspace_id=wsid,
            target_type="context_page",
            target_id=uuid4(),  # no such page (deleted)
            rule_id="knowledge.missing_backlink",
            severity="info",
            status="open",
            fingerprint="m02:dangling",
            message_args={"slug": "deleted-secret"},
        )
        session.add(finding)
        session.flush()
        finding_id = finding.id
        owner = ActorContext(
            actor_kind="system",
            actor_id="owner",
            scopes=("*",),
            workspace_id=wsid,
            workspace_role="owner",
            role="owner",
        )
        # Even an owner does not get a dangling finding we cannot re-authorize.
        listed = {f.id for f in svc.list_findings(session, wsid, actor=owner).findings}
        assert finding_id not in listed
        with pytest.raises(ReadNotFoundError):
            svc.decide_finding(
                session,
                wsid,
                finding_id,
                LintFindingDecisionRequest(decision="acknowledge"),
                actor_kind="system",
                actor_id="owner",
                actor=owner,
            )


def test_metadata_only_finding_survives_runtime_reconciliation(session_factory):
    with session_factory.begin() as session:
        wsid = _ws(session)
        run = LintRun(
            workspace_id=wsid,
            lint_scope={"kind": "maintenance"},
            trigger_type="wiki_maintenance",
            status="completed",
            finding_count_by_severity={"info": 1},
        )
        session.add(run)
        session.flush()
        finding = LintFinding(
            lint_run_id=run.id,
            workspace_id=wsid,
            target_type="workspace_tree_node",
            target_id=None,
            rule_id="maintenance.nav_summary_suggestion",
            severity="info",
            status="open",
            fingerprint="external:test",
        )
        session.add(finding)
        session.flush()
        runtime_policy = {
            rule.rule_id: False
            for rule in LintRegistry.builtin().rules()
            if getattr(rule, "runtime_checkable", True)
        }

        LintService().run_lint(
            session,
            wsid,
            LintRunCreateRequest(),
            "human",
            "test-user",
            policy=runtime_policy,
        )
        session.refresh(finding)
        listed = LintService().list_findings(
            session,
            wsid,
            layer="maintenance",
            status="open",
        )

    assert finding.status == "open"
    assert [item.id for item in listed.findings] == [finding.id]


def test_run_lint_executes_rules_and_persists_findings(session_factory):
    service = LintService()
    with session_factory.begin() as session:
        wsid = _ws(session)
        _page(session, wsid)
        run = service.run_lint(
            session,
            wsid,
            LintRunCreateRequest(lint_scope="workspace", trigger_type="manual"),
            actor_kind="human",
            actor_id="t",
            rules=[_Dummy()],
        )
    assert run.status == "completed"
    assert run.finding_count_by_severity == {"warning": 1}

    with session_factory() as session:
        findings = session.scalars(
            select(LintFinding).where(LintFinding.workspace_id == _ws(session))
        ).all()
    assert len(findings) == 1
    assert findings[0].rule_id == "test.dummy_format"
    assert findings[0].suggested_action == "dummy hit"
    assert findings[0].status == "open"

    with session_factory() as session:
        listed = service.list_findings(session, _ws(session), rule="test.dummy_format")
    assert listed.findings[0].message_key == "test.dummy_format"
    assert listed.findings[0].message_args == {}
    assert listed.findings[0].target_title == "P1"


def test_run_lint_invokes_folder_summary_task_producer(session_factory, monkeypatch):
    calls = []

    def produce(_session, workspace_id):
        calls.append(workspace_id)
        return 0

    monkeypatch.setattr(
        "backend.services.lint_service.produce_folder_summary_tasks", produce
    )
    with session_factory.begin() as session:
        workspace_id = _ws(session)
        LintService().run_lint(
            session,
            workspace_id,
            LintRunCreateRequest(),
            "human",
            "test-user",
            rules=[],
        )

    assert calls == [workspace_id]


def test_missing_backlink_rule_batches_many_outbound_targets(
    session_factory, page_service
):
    targets = [
        page_service.write_page(
            workspace_slug="default",
            slug=f"backlink-target-{index}",
            title=f"Backlink Target {index}",
            body_md=f"# Backlink Target {index}\n\nBody.",
            actor_kind="human",
            actor_id="tester",
            actor_provenance="browser_session",
        )
        for index in range(12)
    ]
    source = page_service.write_page(
        workspace_slug="default",
        slug="backlink-source-many",
        title="Backlink Source Many",
        body_md="# Backlink Source Many\n\n"
        + " ".join(f"[[{target.slug}]]" for target in targets),
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )

    with session_factory() as session:
        workspace_id = _ws(session)
        source_page = session.get(ContextPage, source.id)
        rule = next(
            item
            for item in LintRegistry.builtin().rules()
            if item.rule_id == "kb.missing_backlink"
        )
        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        event.listen(session.get_bind(), "before_cursor_execute", count_query)
        try:
            findings = findings_for_target(
                rule, source_page, LintContext(session, workspace_id, page=source_page)
            )
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", count_query)

    assert len(findings) == len(targets)
    assert query_count <= 2


def test_lint_run_request_rejects_unsupported_scope():
    # J3: only workspace-wide runs are supported; an unknown scope is rejected at
    # the boundary instead of silently linting the whole workspace.
    with pytest.raises(ValidationError):
        LintRunCreateRequest(lint_scope="page:abc")


def test_create_run_delegates_to_run_lint_and_completes(session_factory):
    service = LintService()
    with session_factory.begin() as session:
        wsid = _ws(session)
        run = service.create_run(
            session,
            wsid,
            LintRunCreateRequest(lint_scope="workspace", trigger_type="manual"),
            actor_kind="human",
            actor_id="t",
        )
    assert run.status == "completed"
    assert isinstance(run.finding_count_by_severity, dict)


class _Toggleable:
    stages = frozenset({LintStage.RUN})
    rule_id = "test.toggle"
    layer = "format"
    default_enabled = True
    applies_to = frozenset({"context_page"})
    fires = True

    def check_target(self, target, ctx):
        if not self.fires:
            return []
        return [
            LintFindingDraft(
                rule_id=self.rule_id,
                severity="warning",
                target_type="context_page",
                target_id=target.id,
                suggested_action="toggle hit",
                fingerprint="test.toggle|x",
            )
        ]


def _open(session, rule_id):
    return session.scalars(
        select(LintFinding).where(
            LintFinding.rule_id == rule_id, LintFinding.status == "open"
        )
    ).all()


def test_run_lint_reconciles_fixed_findings_current_state(session_factory):
    # M3: a clean re-run resolves a finding that no longer reproduces, and a
    # still-firing re-run does not duplicate the open finding.
    rule = _Toggleable()
    svc = LintService()
    req = LintRunCreateRequest(lint_scope="workspace", trigger_type="manual")
    with session_factory.begin() as session:
        wsid = _ws(session)
        _page(session, wsid, slug="recon")
        rule.fires = True
        svc.run_lint(session, wsid, req, "human", "t", rules=[rule])
        assert len(_open(session, "test.toggle")) == 1
        # re-run, still firing -> no duplicate open finding
        svc.run_lint(session, wsid, req, "human", "t", rules=[rule])
        assert len(_open(session, "test.toggle")) == 1
        # fix the content -> rule no longer fires -> finding reconciled to fixed
        rule.fires = False
        svc.run_lint(session, wsid, req, "human", "t", rules=[rule])
        assert _open(session, "test.toggle") == []
        fixed = session.scalars(
            select(LintFinding).where(
                LintFinding.rule_id == "test.toggle", LintFinding.status == "fixed"
            )
        ).all()
        assert len(fixed) == 1
        assert fixed[0].resolved_at is not None


def test_run_lint_tombstone_resolves_removed_rule_findings(session_factory):
    # I11: an open finding of a permanently-removed rule (md.frontmatter) is
    # tombstone-resolved on any run, so it does not linger forever on upgraded
    # DBs (the rule can never reproduce to reconcile it the normal way).
    svc = LintService()
    req = LintRunCreateRequest(lint_scope="workspace", trigger_type="manual")
    with session_factory.begin() as session:
        wsid = _ws(session)
        page = _page(session, wsid, slug="tomb")
        run = svc.run_lint(session, wsid, req, "human", "t", rules=[])
        session.add(
            LintFinding(
                lint_run_id=run.id,
                workspace_id=wsid,
                target_type="context_page",
                target_id=page.id,
                rule_id="md.frontmatter",
                severity="error",
                status="open",
                suggested_action="legacy frontmatter finding",
                fingerprint="md.frontmatter|legacy",
            )
        )
        session.flush()
        # a fresh run tombstone-resolves the orphaned removed-rule finding
        svc.run_lint(session, wsid, req, "human", "t", rules=[])
        leftover = session.scalars(
            select(LintFinding).where(LintFinding.rule_id == "md.frontmatter")
        ).one()
        assert leftover.status == "fixed"
        assert leftover.resolved_at is not None


def test_decide_finding_rejects_changes_after_terminal_resolution(session_factory):
    # Human terminal decisions are immutable; resolved_at must remain set.
    rule = _Toggleable()
    rule.fires = True
    svc = LintService()
    req = LintRunCreateRequest(lint_scope="workspace", trigger_type="manual")
    with session_factory.begin() as session:
        wsid = _ws(session)
        _page(session, wsid, slug="lg-f6")
        svc.run_lint(session, wsid, req, "human", "t", rules=[rule])
        finding = session.scalars(
            select(LintFinding).where(LintFinding.rule_id == "test.toggle")
        ).one()
        finding.severity = "error"
        session.flush()

        svc.decide_finding(
            session,
            wsid,
            finding.id,
            LintFindingDecisionRequest(decision="dismiss", reason="x"),
            "human",
            "t",
            actor=None,
        )
        session.refresh(finding)
        assert finding.status == "dismissed"
        assert finding.resolved_at is not None

        # Verify that an idempotent retry still rejects a blank reason for an error
        with pytest.raises(ReadValidationError, match="a reason is required"):
            svc.decide_finding(
                session,
                wsid,
                finding.id,
                LintFindingDecisionRequest(decision="dismiss", reason=""),
                "human",
                "t",
                actor=None,
            )

        with pytest.raises(ReadValidationError):
            svc.decide_finding(
                session,
                wsid,
                finding.id,
                LintFindingDecisionRequest(decision="acknowledge", reason="y"),
                "human",
                "t",
                actor=None,
            )
        session.refresh(finding)
        assert finding.status == "dismissed"
        assert finding.resolved_at is not None


def test_list_findings_status_aliases_and_id_lookup(session_factory):
    rule = _Toggleable()
    rule.fires = True
    svc = LintService()
    request = LintRunCreateRequest(lint_scope="workspace", trigger_type="manual")
    with session_factory.begin() as session:
        wsid = _ws(session)
        _page(session, wsid, slug="status-alias")
        run = svc.run_lint(session, wsid, request, "human", "t", rules=[rule])
        finding = session.scalar(
            select(LintFinding).where(LintFinding.rule_id == "test.toggle")
        )
        finding.status = "acknowledged"
        session.flush()

        open_ids = [
            item.id for item in svc.list_findings(session, wsid, status="open").findings
        ]
        assert open_ids == [finding.id]
        assert svc.list_findings(session, wsid, status="resolved").findings == []
        exact = svc.list_findings(
            session,
            wsid,
            status="resolved",
            severity="error",
            layer="maintenance",
            finding_id=finding.id,
        )
        assert exact.findings[0].id == finding.id
        assert svc.get_run(session, wsid, run.id, actor=None).id == run.id


def test_finding_id_lookup_still_honors_the_disabled_rule_policy_gate(session_factory):
    """A deep link bypasses the caller's list filters, not M11/DEC-4: a finding of
    a rule the operator switched off stays hidden even when addressed by id."""

    rule = _Toggleable()
    rule.fires = True
    registry = LintRegistry(
        (
            *LintRegistry.builtin().modules,
            LintModule("test", 1000, (rule,)),
        )
    )
    svc = LintService(lint_registry=registry)
    request = LintRunCreateRequest(lint_scope="workspace", trigger_type="manual")
    with session_factory.begin() as session:
        wsid = _ws(session)
        _page(session, wsid, slug="policy-gated-link")
        svc.run_lint(session, wsid, request, "human", "t", rules=[rule])
        finding = session.scalar(
            select(LintFinding).where(LintFinding.rule_id == "test.toggle")
        )
        assert svc.list_findings(session, wsid, finding_id=finding.id).findings

        session.get(Workspace, wsid).lint_policy = {"test.toggle": False}
        session.flush()

        assert svc.list_findings(session, wsid, finding_id=finding.id).findings == []


def test_run_lint_does_not_resurrect_dismissed_finding(session_factory):
    # M3: a human decision (dismissed) survives a re-run that still produces the
    # same fingerprint — no new open finding is created.
    rule = _Toggleable()
    rule.fires = True
    svc = LintService()
    req = LintRunCreateRequest(lint_scope="workspace", trigger_type="manual")
    with session_factory.begin() as session:
        wsid = _ws(session)
        _page(session, wsid, slug="dism")
        svc.run_lint(session, wsid, req, "human", "t", rules=[rule])
        finding = session.scalars(
            select(LintFinding).where(LintFinding.rule_id == "test.toggle")
        ).one()
        finding.status = "dismissed"
        session.flush()
        svc.run_lint(session, wsid, req, "human", "t", rules=[rule])
        rows = session.scalars(
            select(LintFinding).where(LintFinding.rule_id == "test.toggle")
        ).all()
    assert len(rows) == 1
    assert rows[0].status == "dismissed"


class _FmtRule:
    stages = frozenset({LintStage.RUN})
    rule_id = "test.fmt"
    layer = "format"
    default_enabled = True
    applies_to = frozenset({"context_page"})

    def check_target(self, target, ctx):
        return [
            LintFindingDraft(
                rule_id=self.rule_id,
                severity="warning",
                target_type="context_page",
                target_id=target.id,
            )
        ]


class _KbRule:
    stages = frozenset({LintStage.RUN})
    rule_id = "test.kb"
    layer = "knowledge"
    default_enabled = True
    applies_to = frozenset({"context_page"})

    def check_target(self, target, ctx):
        return [
            LintFindingDraft(
                rule_id=self.rule_id,
                severity="info",
                target_type="context_page",
                target_id=target.id,
            )
        ]


def test_run_lint_tombstones_removed_near_duplicate_verdict_findings(session_factory):
    """Q-5 now enriches the source kb.semantic_near_duplicate finding, so the old
    verdict rule can never reproduce. Its rows would otherwise linger on upgraded
    DBs — outside every layer filter, forever open."""

    svc = LintService()
    with session_factory.begin() as session:
        wsid = _ws(session)
        run = LintRun(
            workspace_id=wsid,
            lint_scope={"kind": "maintenance"},
            trigger_type="wiki_maintenance",
            status="completed",
            finding_count_by_severity={"info": 1},
        )
        session.add(run)
        session.flush()
        stale = LintFinding(
            lint_run_id=run.id,
            workspace_id=wsid,
            target_type="context_page",
            target_id=None,
            rule_id="maintenance.near_duplicate_verdict",
            severity="info",
            status="open",
            fingerprint="cdt:legacy",
        )
        session.add(stale)
        session.flush()

        svc.run_lint(
            session,
            wsid,
            LintRunCreateRequest(lint_scope="workspace", trigger_type="manual"),
            "human",
            "t",
            rules=[],
        )
        session.refresh(stale)
        assert stale.status == "fixed"
        assert stale.resolved_at is not None


def test_list_findings_layer_filter(session_factory):
    registry = LintRegistry(
        (
            *LintRegistry.builtin().modules,
            LintModule(
                "test",
                1000,
                (
                    _FmtRule(),
                    _KbRule(),
                ),
            ),
        )
    )
    lint = LintService(lint_registry=registry)
    with session_factory.begin() as session:
        wsid = _ws(session)
        _page(session, wsid, slug="layerp")
        lint.create_run(
            session,
            wsid,
            LintRunCreateRequest(lint_scope="workspace", trigger_type="manual"),
            "human",
            "t",
        )
        fmt = lint.list_findings(session, wsid, layer="format")
        kb = lint.list_findings(session, wsid, layer="knowledge")
    fmt_ids = {f.rule_id for f in fmt.findings}
    kb_ids = {f.rule_id for f in kb.findings}
    assert "test.fmt" in fmt_ids and "test.kb" not in fmt_ids
    assert "test.kb" in kb_ids and "test.fmt" not in kb_ids


def _api_client(page_service):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(client)
    return client


def test_lint_run_and_findings_api_smoke(page_service):
    client = _api_client(page_service)
    wsid = client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]

    run = client.post(
        f"/api/v1/workspaces/{wsid}/lint/runs",
        json={"lint_scope": "workspace", "trigger_type": "manual"},
    )
    assert run.status_code == 202, run.text
    assert run.json()["data"]["status"] == "queued"
    run_id = run.json()["data"]["id"]
    fetched_run = client.get(f"/api/v1/workspaces/{wsid}/lint/runs/{run_id}")
    assert fetched_run.status_code == 200
    assert fetched_run.json()["data"]["id"] == run_id

    findings = client.get(
        f"/api/v1/workspaces/{wsid}/lint/findings?layer=format&page=1&page_size=20"
    )
    assert findings.status_code == 200
    findings_data = findings.json()["data"]
    assert "findings" in findings_data
    assert findings_data["page"] == 1
    assert findings_data["page_size"] == 20
    assert findings_data["page_count"] == 0
    assert findings_data["has_previous"] is False
    assert findings_data["has_next"] is False
    assert findings_data["next_cursor"] is None

    mixed_modes = client.get(
        f"/api/v1/workspaces/{wsid}/lint/findings?page=1&page_size=20&limit=50"
    )
    assert mixed_modes.status_code == 422
    assert mixed_modes.json()["error_code"] == "validation_failed"
    assert "cannot be combined" in mixed_modes.json()["message"]


def test_list_lint_rules_catalog(session_factory):
    lint = LintService()
    with session_factory.begin() as session:
        data = lint.list_rules(session, _ws(session))
    by_id = {r.rule_id: r for r in data.rules}
    assert "md.code_fence_balance" in by_id and "kb.orphan_page" in by_id
    assert by_id["md.code_fence_balance"].layer == "format"
    assert by_id["md.code_fence_balance"].enabled is True  # format default on
    assert by_id["kb.orphan_page"].layer == "knowledge"
    assert by_id["kb.orphan_page"].enabled is True  # Sprint-6: knowledge default on


def test_list_lint_rules_reflects_policy_override(session_factory):
    lint = LintService()
    with session_factory.begin() as session:
        ws = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        ws.lint_policy = {"kb.orphan_page": False}
        session.flush()
        data = lint.list_rules(session, ws.id)
    by_id = {r.rule_id: r for r in data.rules}
    assert by_id["kb.orphan_page"].enabled is False
    assert by_id["kb.orphan_page"].policy_locked is False


def test_lint_rules_api_smoke(page_service):
    client = _api_client(page_service)
    wsid = client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    resp = client.get(f"/api/v1/workspaces/{wsid}/lint/rules")
    assert resp.status_code == 200
    rules = resp.json()["data"]["rules"]
    assert any(r["rule_id"] == "md.code_fence_balance" for r in rules)


def test_lint_format_endpoint_normalizes(page_service):
    # F2d: on-demand format returns the normalized Markdown (stateless).
    client = _api_client(page_service)
    wsid = client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    resp = client.post(
        f"/api/v1/workspaces/{wsid}/lint/format",
        json={"markdown": "# Title  \n\n* item\n\n\n\nend   \n"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["markdown"] == "# Title\n\n- item\n\nend\n"


def test_lint_format_rejects_oversized_body(page_service):
    # I9: the formatter caps input at BODY_MD_MAX_LENGTH like the write APIs.
    from backend.schemas.page import BODY_MD_MAX_LENGTH

    client = _api_client(page_service)
    wsid = client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    resp = client.post(
        f"/api/v1/workspaces/{wsid}/lint/format",
        json={"markdown": "a" * (BODY_MD_MAX_LENGTH + 1)},
    )
    assert resp.status_code == 422, resp.text


def test_lint_format_failclosed_on_pathological_table(page_service):
    # I1: a ragged/escaped-pipe table never 500s — it returns the body unchanged.
    client = _api_client(page_service)
    wsid = client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    body = "| a | b | c |\n| --- | --- |\n| x | y | z |\n"
    resp = client.post(
        f"/api/v1/workspaces/{wsid}/lint/format",
        json={"markdown": body},
    )
    assert resp.status_code == 200, resp.text


def test_lint_rules_unknown_workspace_is_authz_rejected(page_service):
    # Missing and private non-member workspaces share the uniform not-found
    # response so the authorization boundary cannot enumerate workspace ids.
    client = _api_client(page_service)
    missing = "00000000-0000-0000-0000-0000000000aa"
    resp = client.get(f"/api/v1/workspaces/{missing}/lint/rules")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "not_found"


def test_batch_decide_findings_comprehensive(session_factory):
    svc = LintService()
    with session_factory.begin() as session:
        wsid = _ws(session)
        # Create a real page to test invariance
        pg = _page(session, wsid, slug="target")
        pg_id = pg.id
        # Let's just track the whole pg dict or check version
        pg_version = pg.current_version_id

        run = LintRun(
            workspace_id=wsid,
            lint_scope={"kind": "workspace"},
            trigger_type="manual",
            status="completed",
            finding_count_by_severity={},
        )
        session.add(run)
        session.flush()

        finding1 = LintFinding(
            lint_run_id=run.id,
            workspace_id=wsid,
            target_type="workspace",
            rule_id="test_rule",
            severity="error",
            status="open",
            fingerprint="f1",
        )
        finding2 = LintFinding(
            lint_run_id=run.id,
            workspace_id=wsid,
            target_type="workspace",
            rule_id="test_rule",
            severity="warning",
            status="open",
            fingerprint="f2",
        )
        session.add_all([finding1, finding2])
        session.flush()

        f1_id = finding1.id
        f2_id = finding2.id
        invalid_id = uuid4()

        actor = ActorContext(
            actor_kind="system",
            actor_id="owner",
            token_kind="internal",
            scopes=("*",),
            workspace_role="owner",
            role="owner",
        )

        # 1. Test error dismiss requires reason
        req_no_reason = LintFindingBatchDecisionRequest(
            finding_ids=[f1_id, f2_id], decision="dismiss", reason=""
        )
        res_err = svc.batch_decide_findings(
            session,
            wsid,
            req_no_reason,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor=actor,
        )
        # finding1 (error) fails, finding2 (warning) succeeds
        assert len(res_err["updated"]) == 1
        assert res_err["updated"][0].id == f2_id
        assert res_err["updated"][0].status == "dismissed"
        assert len(res_err["failed"]) == 1
        assert res_err["failed"][0].id == f1_id
        assert res_err["failed"][0].error_code == "validation_failed"

        # 2. Preserve request order across a successful item and two failures.
        req = LintFindingBatchDecisionRequest(
            finding_ids=[f1_id, invalid_id, f2_id], decision="acknowledge"
        )

        # Batch 1 (f2 is already dismissed, f1 is open)
        res1 = svc.batch_decide_findings(
            session,
            wsid,
            req,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor=actor,
        )
        # f1 updates; invalid_id fails not-found; f2 is already terminal.
        assert len(res1["updated"]) == 1, (
            f"Expected 1 update, got {len(res1['updated'])}. Failures: {res1['failed']}"
        )
        assert res1["updated"][0].id == f1_id
        assert len(res1["failed"]) == 2

        # Batch 2 idempotency check
        res2 = svc.batch_decide_findings(
            session,
            wsid,
            req,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor=actor,
        )
        assert [item.id for item in res2["updated"]] == [f1_id]
        assert [item.id for item in res2["failed"]] == [invalid_id, f2_id]

        # 3. Page version invariance
        pg_after = session.get(ContextPage, pg_id)
        assert pg_after.current_version_id == pg_version


def test_batch_decide_api(page_service, session_factory):
    client = _api_client(page_service)
    wsid = client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]

    # 4. Limit/duplicate validation
    # Send empty list
    resp = client.post(
        f"/api/v1/workspaces/{wsid}/lint/findings/batch/decision",
        json={"finding_ids": [], "decision": "acknowledge"},
    )
    assert resp.status_code == 422  # min_length=1

    # Send >100
    many_ids = [str(uuid4()) for _ in range(101)]
    resp = client.post(
        f"/api/v1/workspaces/{wsid}/lint/findings/batch/decision",
        json={"finding_ids": many_ids, "decision": "acknowledge"},
    )
    assert resp.status_code == 422  # max_length=100

    # Duplicates are rejected at the request boundary so one client mistake
    # cannot produce duplicate audit entries or order-dependent results.
    dup_id = str(uuid4())
    resp = client.post(
        f"/api/v1/workspaces/{wsid}/lint/findings/batch/decision",
        json={"finding_ids": [dup_id, dup_id], "decision": "acknowledge"},
    )
    assert resp.status_code == 422

    # Status-only "Record as fixed" is intentionally excluded from bulk.
    resp = client.post(
        f"/api/v1/workspaces/{wsid}/lint/findings/batch/decision",
        json={"finding_ids": [str(uuid4())], "decision": "fix"},
    )
    assert resp.status_code == 422

    # A valid request must reach the literal batch route, not be captured by
    # the later /{finding_id}/decision UUID route.
    with session_factory.begin() as session:
        run = LintRun(
            workspace_id=UUID(wsid),
            lint_scope={"kind": "workspace"},
            trigger_type="manual",
            status="completed",
            finding_count_by_severity={},
        )
        session.add(run)
        session.flush()
        finding = LintFinding(
            lint_run_id=run.id,
            workspace_id=UUID(wsid),
            target_type="workspace",
            rule_id="test.batch.route",
            severity="warning",
            status="open",
            fingerprint="batch-route",
        )
        session.add(finding)
        session.flush()
        finding_id = finding.id

    resp = client.post(
        f"/api/v1/workspaces/{wsid}/lint/findings/batch/decision",
        json={"finding_ids": [str(finding_id)], "decision": "acknowledge"},
    )
    assert resp.status_code == 200, resp.text
    assert [item["id"] for item in resp.json()["data"]["updated"]] == [str(finding_id)]
    assert resp.json()["data"]["failed"] == []


def test_lint_decision_viewer_rbac_403(page_service, session_factory):
    client = _api_client(page_service)
    wsid_str = client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    wsid = UUID(wsid_str)
    with session_factory.begin() as session:
        run = LintRun(
            workspace_id=wsid,
            lint_scope={"kind": "workspace"},
            trigger_type="manual",
            status="completed",
            finding_count_by_severity={},
        )
        session.add(run)
        session.flush()
        finding = LintFinding(
            lint_run_id=run.id,
            workspace_id=wsid,
            target_type="workspace",
            rule_id="test_rule",
            severity="error",
            status="open",
            fingerprint="f1",
        )
        session.add(finding)
        session.flush()
        f1_id = finding.id

        viewer_id = uuid4()
        session.add(
            User(id=viewer_id, email="v@v.v", display_name="v", password_hash="dummy")
        )
        session.add(
            WorkspaceMembership(workspace_id=wsid, user_id=viewer_id, role="viewer")
        )
        session.flush()

    # The fixture client is an editor by default; override it with a viewer.
    viewer = ActorContext(
        actor_kind="human",
        actor_id=str(viewer_id),
        user_id=viewer_id,
        token_kind="session",
        scopes=("read",),
        workspace_id=wsid,
        workspace_role="viewer",
        role="viewer",
    )
    client.app.dependency_overrides[require_token_scope] = lambda: viewer

    resp_single = client.post(
        f"/api/v1/workspaces/{wsid}/lint/findings/{f1_id}/decision",
        json={"decision": "acknowledge"},
    )
    assert resp_single.status_code == 403, resp_single.text

    resp_batch = client.post(
        f"/api/v1/workspaces/{wsid}/lint/findings/batch/decision",
        json={"finding_ids": [str(f1_id)], "decision": "acknowledge"},
    )
    assert resp_batch.status_code == 403, resp_batch.text
