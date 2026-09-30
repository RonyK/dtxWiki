"""Lint plugin engine package.

Public API lives in :mod:`backend.services.lint.base`. Rule plugin modules are
imported here so they self-register into the registry whenever the package is
imported.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from backend.services.lint.base import (
    LINT_LAYERS,
    LINT_SEVERITIES,
    LintFindingDraft,
    LintRule,
    all_rules,
    enabled_rules,
    get_rule,
    known_rule_ids,
    register_rule,
)

from . import (
    rules_format,  # noqa: F401  (self-registers format rules)
    rules_knowledge,  # noqa: F401  (self-registers knowledge rules)
    rules_links,  # noqa: F401  (self-registers link-integrity rules)
    rules_metadata,  # noqa: F401  (registers externally-fed rule metadata)
    rules_nav,  # noqa: F401  (self-registers navigation rules)
)


def page_format_warnings_for_body(
    session: Session,
    workspace_id: UUID,
    page: object,
    body: str | None,
    *,
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
    for rule in enabled_rules(policy, layer="format", target_type="context_page"):
        for draft in rule.check(
            session=session, workspace_id=workspace_id, target=page, body=body
        ):
            messages.append(draft.suggested_action or draft.rule_id)
    return messages


__all__ = [
    "LINT_LAYERS",
    "LINT_SEVERITIES",
    "LintFindingDraft",
    "LintRule",
    "all_rules",
    "enabled_rules",
    "get_rule",
    "known_rule_ids",
    "page_format_warnings_for_body",
    "register_rule",
]
