"""Lint rule contracts, immutable modules, and workspace policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
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


class LintStage(StrEnum):
    SAVE = "save"
    READ = "read"
    RUN = "run"
    PUBLISH_FLOOR = "publish_floor"


@dataclass(frozen=True)
class LintContext:
    """The caller's workspace, actor, and optional page/version context."""

    session: Session | None
    workspace_id: UUID | None
    actor: object | None = None
    page: object | None = None
    version: object | None = None


class LintRule(Protocol):
    rule_id: str
    layer: str
    default_enabled: bool
    applies_to: frozenset[str]
    stages: frozenset[LintStage]


@runtime_checkable
class BodyRule(LintRule, Protocol):
    def check_body(self, body: str, ctx: LintContext) -> list[LintFindingDraft]: ...


@runtime_checkable
class TargetRule(LintRule, Protocol):
    def check_target(
        self, target: object, ctx: LintContext
    ) -> list[LintFindingDraft]: ...


@dataclass(frozen=True)
class LintModule:
    """An explicit, ordered group of lint rules supplied to a registry."""

    module_id: str
    order: int
    rules: tuple[LintRule, ...]


class LintPolicy:
    """Resolve workspace rule toggles and their configured defaults."""

    def __init__(self, workspace: object | None) -> None:
        self._overrides = dict(getattr(workspace, "lint_policy", None) or {})

    @classmethod
    def from_overrides(cls, overrides: dict[str, bool] | None) -> LintPolicy:
        policy = cls(None)
        policy._overrides = dict(overrides or {})
        return policy

    def rule_enabled(self, rule_id: str, *, default_enabled: bool = True) -> bool:
        return self._overrides.get(rule_id, default_enabled)
