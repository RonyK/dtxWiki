from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.services.lint import LintContext, LintRegistry
from backend.services.lint.scopes import findings_for_target

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _folder(session, workspace_id, slug, *, parent_id=None, visible=True):
    node = WorkspaceTreeNode(
        workspace_id=workspace_id,
        section="context",
        node_kind="folder",
        parent_id=parent_id,
        sort_key=slug,
        title=slug,
        slug=slug,
        target_kind=None,
        target_id=None,
        visibility_status="visible" if visible else "archived",
    )
    session.add(node)
    session.flush()
    return node


def _check(session, rule_id, target):
    return findings_for_target(
        LintRegistry.builtin().rule(rule_id),
        target,
        LintContext(session, target.workspace_id, page=target),
    )


def test_nav_fanout_and_missing_summary_boundary_values(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        fanout = _folder(session, workspace_id, "fanout")
        summary = _folder(session, workspace_id, "summary")
        for index in range(50):
            _folder(session, workspace_id, f"fanout-{index}", parent_id=fanout.id)
        for index in range(9):
            _folder(session, workspace_id, f"summary-{index}", parent_id=summary.id)

        assert _check(session, "nav.fanout_exceeded", fanout) == []
        assert _check(session, "nav.missing_summary", summary) == []

        _folder(session, workspace_id, "fanout-50", parent_id=fanout.id)
        _folder(session, workspace_id, "summary-9", parent_id=summary.id)
        fanout_findings = _check(session, "nav.fanout_exceeded", fanout)
        summary_findings = _check(session, "nav.missing_summary", summary)

        summary.nav_summary = "Queries about the summary folder belong here."
        assert _check(session, "nav.missing_summary", summary) == []

    assert len(fanout_findings) == 1
    assert fanout_findings[0].severity == "warning"
    assert len(summary_findings) == 1
    assert summary_findings[0].severity == "info"


def test_nav_unreachable_detects_hidden_parent_and_missing_primary_placement(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _folder(session, workspace_id, "reachable-root")
        hidden_parent = _folder(session, workspace_id, "hidden-parent", visible=False)
        stranded = _folder(
            session,
            workspace_id,
            "stranded",
            parent_id=hidden_parent.id,
        )
        page = ContextPage(
            workspace_id=workspace_id,
            slug=f"unplaced-{uuid4().hex[:8]}",
            title="Unplaced",
            page_type="concept",
        )
        session.add(page)
        session.flush()

        assert _check(session, "nav.unreachable", root) == []
        stranded_findings = _check(session, "nav.unreachable", stranded)
        page_findings = _check(session, "nav.unreachable", page)

    assert len(stranded_findings) == 1
    assert stranded_findings[0].target_type == "workspace_tree_node"
    assert len(page_findings) == 1
    assert page_findings[0].target_type == "context_page"
