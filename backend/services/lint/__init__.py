"""Explicit lint rule contracts and registry."""

from uuid import UUID

from sqlalchemy.orm import Session

from backend.services.lint.base import (
    LINT_LAYERS,
    LINT_SEVERITIES,
    LintFindingDraft,
    LintModule,
    LintPolicy,
    LintRule,
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
    for rule in registry.enabled(policy, layer="format", target_type="context_page"):
        for draft in rule.check(
            session=session, workspace_id=workspace_id, target=page, body=body
        ):
            messages.append(draft.suggested_action or draft.rule_id)
    return messages


__all__ = [
    "LINT_LAYERS",
    "LINT_SEVERITIES",
    "LintFindingDraft",
    "LintModule",
    "LintPolicy",
    "LintRegistry",
    "LintRule",
    "page_format_warnings_for_body",
]
