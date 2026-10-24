"""Lint rule contracts, immutable modules, and workspace policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.orm import Session

from backend.domain.lint import LintFindingDraft as LintFindingDraft

if TYPE_CHECKING:
    from backend.models.context import ContextPage, ContextPageVersion

LINT_LAYERS = ("format", "knowledge", "nav", "maintenance")
LINT_SEVERITIES = ("error", "warning", "info")


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
    page: ContextPage | None = None
    version: ContextPageVersion | None = None
    # Parser results live only for this invocation and key on the exact body.
    body_analysis: dict[tuple[str, str], object] = field(
        default_factory=dict, repr=False, compare=False
    )


@dataclass(frozen=True)
class FixResult:
    body: str
    fixes: tuple[str, ...] = ()


class LintFixer(Protocol):
    """A meaning-preserving, idempotent transform that fails closed."""

    def fix(self, body: str, ctx: LintContext) -> FixResult: ...


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


@runtime_checkable
class VersionScopedTargetRule(TargetRule, Protocol):
    """A target rule evaluated once for each visible Context version."""

    version_scoped: bool


@dataclass(frozen=True)
class LintModule:
    """An explicit, ordered group of lint rules supplied to a registry."""

    module_id: str
    order: int
    rules: tuple[LintRule, ...]
    fixer: LintFixer | None = None
    fix_on_save_setting: str | None = None


class LintPolicy:
    """Resolve workspace rule toggles and their configured defaults."""

    def __init__(self, workspace: object | None) -> None:
        self.workspace = workspace
        self._overrides = dict(getattr(workspace, "lint_policy", None) or {})

    @classmethod
    def from_overrides(cls, overrides: dict[str, bool] | None) -> LintPolicy:
        policy = cls(None)
        policy._overrides = dict(overrides or {})
        return policy

    def rule_enabled(self, rule_id: str, *, default_enabled: bool) -> bool:
        return self._overrides.get(rule_id, default_enabled)

    def fix_on_save(self, module: LintModule) -> bool:
        if module.fix_on_save_setting == "auto_fix":
            return bool(getattr(self.workspace, "auto_fix_enabled", False))
        return bool(self._overrides.get(module.fix_on_save_setting, False))
