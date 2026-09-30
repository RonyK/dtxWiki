"""Lint plugin engine — base protocol + registry (plan A-T1).

A ``LintRule`` is a pluggable checker that inspects a *target* (e.g. a
ContextPage) and yields :class:`LintFindingDraft` rows. Rules self-register into
the module registry at import time via :func:`register_rule`. Per-workspace
enablement is a workspace ``lint`` policy (``{rule_id: bool}``) that overrides
each rule's ``default_enabled``.

This module is intentionally dependency-light: it knows nothing about the lint
ORM models or the DB. ``LintService.run_lint`` (A-T2) maps the drafts produced
here onto ``LintFinding`` rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

LINT_LAYERS = ("format", "knowledge", "nav", "maintenance")
LINT_SEVERITIES = ("error", "warning", "info")


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


class LintRule(Protocol):
    """A pluggable lint checker.

    ``layer`` is ``"format"`` (deterministic, syntactic) or ``"knowledge"``
    (semantic / graph). ``applies_to`` is the set of target types the rule can
    check; the executor only hands a rule targets of a matching type.
    """

    rule_id: str
    layer: str
    default_enabled: bool
    applies_to: frozenset[str]
    runtime_checkable: bool

    def check(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        target: object,
        body: str | None = None,
    ) -> list[LintFindingDraft]:
        """Produce findings for ``target``. ``body`` lets a caller lint an
        explicit body (e.g. a specific version) instead of the rule's default
        page-body resolution; rules that don't read a body may ignore it."""
        ...


_REGISTRY: dict[str, LintRule] = {}


def register_rule(rule: LintRule) -> LintRule:
    """Register a rule; raises on a duplicate ``rule_id``. Returns the rule so it
    can be used as a decorator-like one-liner at module scope."""
    if rule.layer not in LINT_LAYERS:
        raise ValueError(f"unknown lint layer {rule.layer!r} for {rule.rule_id}")
    if not isinstance(rule.default_enabled, bool):
        raise ValueError(f"default_enabled must be bool for {rule.rule_id}")
    # An empty applies_to would register cleanly but be silently excluded by the
    # target_type filter in enabled_rules and never run — fail fast instead (J2).
    if not rule.applies_to:
        raise ValueError(f"applies_to must be non-empty for {rule.rule_id}")
    if rule.rule_id in _REGISTRY:
        raise ValueError(f"duplicate lint rule_id: {rule.rule_id}")
    _REGISTRY[rule.rule_id] = rule
    return rule


def all_rules() -> list[LintRule]:
    return list(_REGISTRY.values())


def get_rule(rule_id: str) -> LintRule | None:
    return _REGISTRY.get(rule_id)


def known_rule_ids() -> set[str]:
    return set(_REGISTRY)


# The rules the publication floor depends on. A workspace may not switch these
# off: the finding would disappear from the Lint screen while saves were still
# held by the same check, which reads as a bug rather than a policy.
PUBLICATION_FLOOR_RULES = frozenset({"md.code_fence_balance"})


def enabled_rules(
    policy: dict[str, bool] | None,
    *,
    rules: list[LintRule] | None = None,
    layer: str | None = None,
    target_type: str | None = None,
) -> list[LintRule]:
    """Resolve the rules that should run.

    ``policy`` is the workspace ``lint`` policy (``{rule_id: bool}``); a rule with
    no policy entry falls back to its ``default_enabled``. ``rules`` defaults to
    the global registry but can be injected for testing. ``layer`` /
    ``target_type`` narrow the set.
    """

    policy = policy or {}
    candidates = rules if rules is not None else all_rules()
    out: list[LintRule] = []
    for rule in candidates:
        if not getattr(rule, "runtime_checkable", True):
            continue
        if layer is not None and rule.layer != layer:
            continue
        if target_type is not None and target_type not in rule.applies_to:
            continue
        if rule.rule_id in PUBLICATION_FLOOR_RULES:
            # A required publishing safeguard runs whatever the policy says.
            # The floor holds saves through this same check, so letting a
            # workspace hide the finding would leave the author staring at a
            # page that will not publish and a Lint screen that reports nothing.
            out.append(rule)
            continue
        if policy.get(rule.rule_id, rule.default_enabled):
            out.append(rule)
    return out
