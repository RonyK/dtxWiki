from __future__ import annotations

from collections import defaultdict, deque

from sqlalchemy import func, select

from backend.models.context import ContextPage
from backend.models.tree import WorkspaceTreeNode
from backend.services.lint.base import (
    LintContext,
    LintFindingDraft,
    LintModule,
    LintStage,
)


def _draft(rule_id, severity, target_type, target_id, message, message_args=None):
    return LintFindingDraft(
        rule_id=rule_id,
        severity=severity,
        target_type=target_type,
        target_id=target_id,
        suggested_action=message,
        message_key=rule_id,
        message_args=message_args or {},
        fingerprint=f"{rule_id}|",
    )


class _FanoutExceededRule:
    rule_id = "nav.fanout_exceeded"
    layer = "nav"
    default_enabled = True
    applies_to = frozenset({"workspace_tree_node"})
    stages = frozenset({LintStage.RUN})

    def check_target(self, target, ctx: LintContext):
        session, workspace_id = ctx.session, ctx.workspace_id
        if target.node_kind != "folder" or target.visibility_status != "visible":
            return []
        count = session.scalar(
            select(func.count(WorkspaceTreeNode.id)).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.parent_id == target.id,
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )
        if (count or 0) <= 50:
            return []
        return [
            _draft(
                self.rule_id,
                "warning",
                "workspace_tree_node",
                target.id,
                f"Folder has {count} visible children; split it below fanout 50.",
                {"count": count, "limit": 50},
            )
        ]


class _MissingSummaryRule:
    rule_id = "nav.missing_summary"
    layer = "nav"
    default_enabled = True
    applies_to = frozenset({"workspace_tree_node"})
    stages = frozenset({LintStage.RUN})

    def check_target(self, target, ctx: LintContext):
        session, workspace_id = ctx.session, ctx.workspace_id
        if (
            target.node_kind != "folder"
            or target.visibility_status != "visible"
            or (target.nav_summary or "").strip()
        ):
            return []
        count = session.scalar(
            select(func.count(WorkspaceTreeNode.id)).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.parent_id == target.id,
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )
        if (count or 0) < 10:
            return []
        return [
            _draft(
                self.rule_id,
                "info",
                "workspace_tree_node",
                target.id,
                "Add a discriminative navigation summary for this folder.",
                {},
            )
        ]


def _reachable_node_ids(session, workspace_id):
    cache = session.info.setdefault("_lint_nav_reachable", {})
    if workspace_id in cache:
        return cache[workspace_id]
    nodes = session.scalars(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.visibility_status == "visible",
        )
    ).all()
    children = defaultdict(list)
    roots = []
    for node in nodes:
        if node.parent_id is None:
            roots.append(node.id)
        else:
            children[node.parent_id].append(node.id)
    reachable = set(roots)
    queue = deque(roots)
    while queue:
        parent = queue.popleft()
        for child in children[parent]:
            if child not in reachable:
                reachable.add(child)
                queue.append(child)
    cache[workspace_id] = reachable
    return reachable


class _UnreachableRule:
    rule_id = "nav.unreachable"
    layer = "nav"
    default_enabled = True
    applies_to = frozenset({"workspace_tree_node", "context_page"})
    stages = frozenset({LintStage.RUN})

    def check_target(self, target, ctx: LintContext):
        session, workspace_id = ctx.session, ctx.workspace_id
        if isinstance(target, WorkspaceTreeNode):
            if (
                target.visibility_status != "visible"
                or target.id in _reachable_node_ids(session, workspace_id)
            ):
                return []
            return [
                _draft(
                    self.rule_id,
                    "warning",
                    "workspace_tree_node",
                    target.id,
                    "Tree node is not reachable from a visible root.",
                    {},
                )
            ]
        if not isinstance(target, ContextPage) or target.lifecycle_status == "archived":
            return []
        placement = session.scalar(
            select(func.count(WorkspaceTreeNode.id)).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == target.id,
                WorkspaceTreeNode.primary_placement.is_(True),
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )
        if placement:
            return []
        return [
            _draft(
                self.rule_id,
                "warning",
                "context_page",
                target.id,
                "Page has no visible primary tree placement.",
                {},
            )
        ]


MODULE = LintModule(
    "nav",
    400,
    (
        _FanoutExceededRule(),
        _MissingSummaryRule(),
        _UnreachableRule(),
    ),
)
