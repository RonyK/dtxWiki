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
    """Published head first, then shared draft; private editing rows stay out."""
    versions: list[tuple[str, ContextPageVersion]] = []
    seen = set()
    for scope, version_id in (
        ("published", page.current_published_version_id),
        ("draft", page.latest_draft_version_id),
    ):
        if version_id is None or version_id in seen:
            continue
        seen.add(version_id)
        version = session.get(ContextPageVersion, version_id)
        if version is None or (
            scope == "draft" and version.version_status == "editing"
        ):
            continue
        versions.append((scope, version))
    return versions


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
