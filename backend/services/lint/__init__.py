"""Explicit lint rule contracts and registry."""

from uuid import UUID

from sqlalchemy.orm import Session

from backend.services.lint.base import (
    LINT_LAYERS,
    LINT_SEVERITIES,
    BodyRule,
    FixResult,
    LintContext,
    LintFindingDraft,
    LintFixer,
    LintModule,
    LintPolicy,
    LintRule,
    LintStage,
    TargetRule,
    VersionScopedTargetRule,
)
from backend.services.lint.registry import LintRegistry


def page_format_warnings_for_body(
    session: Session,
    workspace_id: UUID,
    page: object,
    body: str | None,
    *,
    registry: LintRegistry,
    policy: dict | None = None,
) -> list[str]:
    """Non-blocking format-layer lint messages for a SPECIFIC body of ``page``.

    Drives the Context inspector (M1): warnings reflect the exact version body
    being displayed (version-aware), not a page-level published/draft guess.
    Human authors see format issues without being blocked (the hard gate lives
    in :mod:`backend.services.auto_approval`). Honors the workspace ``lint``
    toggle policy, so disabled rules contribute nothing.
    """
    if not body:
        return []
    messages: list[str] = []
    ctx = LintContext(session, workspace_id, page=page)
    for rule in registry.rules_for(LintStage.READ, policy, target_type="context_page"):
        for draft in rule.check_body(body, ctx):
            messages.append(draft.suggested_action or draft.rule_id)
    return messages


def publish_floor_findings(
    body: str, *, registry: LintRegistry | None = None
) -> list[LintFindingDraft]:
    """Apply the publication stage's required rules to one submitted body."""
    if not body:
        return []
    resolved_registry = registry if registry is not None else LintRegistry.builtin()
    return [
        finding
        for rule in resolved_registry.rules_for(LintStage.PUBLISH_FLOOR, None)
        for finding in rule.check_body(body, LintContext(None, None))
    ]


__all__ = [
    "LINT_LAYERS",
    "LINT_SEVERITIES",
    "BodyRule",
    "FixResult",
    "LintContext",
    "LintFindingDraft",
    "LintFixer",
    "LintModule",
    "LintPolicy",
    "LintRegistry",
    "LintRule",
    "LintStage",
    "TargetRule",
    "VersionScopedTargetRule",
    "page_format_warnings_for_body",
    "publish_floor_findings",
]
