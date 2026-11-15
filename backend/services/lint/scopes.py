"""Shared lint version visibility and finding reconciliation helpers."""

from collections.abc import Iterable
from dataclasses import replace

from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion
from backend.services.lint.base import (
    BodyRule,
    LintContext,
    LintFindingDraft,
    LintRule,
    VersionScopedTargetRule,
)


def lint_visible_versions(
    session: Session, page: ContextPage
) -> list[tuple[str, ContextPageVersion]]:
    """Inspect the published snapshot and every mutable shared draft."""
    from backend.repositories.workspace_scoped import select_in_workspace

    rows = session.scalars(
        select_in_workspace(
            ContextPageVersion,
            workspace_id=page.workspace_id,
            conditions=(ContextPageVersion.page_id == page.id,),
        )
    ).all()
    by_id = {row.id: row for row in rows}
    versions = (
        [("published", by_id[page.current_published_version_id])]
        if page.current_published_version_id in by_id
        else []
    )
    return versions + [("draft", row) for row in rows if row.version_status == "draft"]


def collapse_by_fingerprint(
    findings_by_scope: Iterable[tuple[str, Iterable[LintFindingDraft]]],
) -> list[LintFindingDraft]:
    """Keep the first representative of each defect and attach its body scopes."""
    collapsed: dict[tuple, LintFindingDraft] = {}
    for scope, findings in findings_by_scope:
        for finding in findings:
            key = (finding.target_id, finding.fingerprint)
            prior = collapsed.get(key)
            if prior is None:
                args = dict(finding.message_args or {})
                args["body_scopes"] = [scope]
                collapsed[key] = replace(finding, message_args=args)
            elif scope not in prior.message_args["body_scopes"]:
                prior.message_args["body_scopes"].append(scope)
    return list(collapsed.values())


def findings_for_target(
    rule: LintRule, target: object, ctx: LintContext
) -> list[LintFindingDraft]:
    """Execute one rule, expanding body/version scopes only when it needs them."""
    body_rule = isinstance(rule, BodyRule)
    if body_rule or (isinstance(rule, VersionScopedTargetRule) and rule.version_scoped):
        return collapse_by_fingerprint(
            (
                scope,
                rule.check_body(
                    version.body_md, replace(ctx, page=target, version=version)
                )
                if body_rule
                else rule.check_target(
                    target, replace(ctx, page=target, version=version)
                ),
            )
            for scope, version in lint_visible_versions(ctx.session, target)
        )
    return rule.check_target(target, ctx)
