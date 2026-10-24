"""Lint finding values shared by preparation and rule execution."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class LintFindingDraft:
    """A single rule hit, before it is persisted as a ``LintFinding``.

    ``LintFinding`` has no free-text message column, so the specific, human-facing
    detail (e.g. "heading level jumps from H1 to H3") goes in
    ``suggested_action``; ``rule_id`` identifies which rule produced it.
    """

    rule_id: str
    severity: str  # one of LINT_SEVERITIES
    target_type: str
    target_id: UUID | None = None
    suggested_action: str | None = None
    message_key: str | None = None
    message_args: dict[str, Any] | None = None
    # Stable identity for current-state reconciliation (M3): excludes volatile
    # line numbers so an acknowledged/dismissed finding survives unrelated edits.
    # Convention: "{rule_id}|{anchor}" where anchor is a rule-specific stable
    # locator (slug, heading text, …) or "" for one-per-target rules.
    fingerprint: str = ""
