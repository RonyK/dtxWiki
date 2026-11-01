"""Lint rule contracts, immutable modules, and workspace policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable
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
    body_analysis: dict[tuple[object, ...], object] = field(
        default_factory=dict, repr=False, compare=False
    )
    stage: LintStage | None = None
    self_origins: frozenset[str] | None = None
    source_version: ContextPageVersion | None = None
    link_source: Literal["draft", "published"] | None = None


@dataclass(frozen=True)
class FixResult:
    body: str
    fixes: tuple[str, ...] = ()
    details: tuple[dict, ...] = ()


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
    auto_approve_setting: str | None = None
    maintenance_role: Literal["none", "trigger", "companion"] = "none"
    fixable_rule_ids: frozenset[str] = frozenset()
    normalizer: LintFixer | None = None
    fix_on_save_default: bool | str = False
    fix_on_save_enabled_value: bool | str = True
    auto_approve_default: bool | str = False
    auto_approve_enabled_value: bool | str = True


class LintPolicy:
    """Resolve workspace rule toggles and their configured defaults."""

    def __init__(self, workspace: object | None) -> None:
        self.workspace = workspace
        self._overrides = dict(getattr(workspace, "lint_policy", None) or {})

    @classmethod
    def from_overrides(cls, overrides: dict[str, bool | str] | None) -> LintPolicy:
        policy = cls(None)
        policy._overrides = dict(overrides or {})
        return policy

    def rule_enabled(self, rule_id: str, *, default_enabled: bool) -> bool:
        return self._overrides.get(rule_id, default_enabled)

    def setting_value(self, setting: str, *, default: bool | str = False) -> object:
        """Read one declared setting; only the legacy column needs an alias."""
        if setting == "auto_fix" and self.workspace is not None:
            return getattr(self.workspace, "auto_fix_enabled", default)
        return self._overrides.get(setting, default)

    def effective_settings(self, modules: Iterable[LintModule]) -> dict[str, object]:
        """Keep stored rule toggles and fill module options from their declarations."""
        values = dict(self._overrides)
        for module in modules:
            for setting, default in (
                (module.fix_on_save_setting, module.fix_on_save_default),
                (module.auto_approve_setting, module.auto_approve_default),
            ):
                if setting is not None:
                    values[setting] = self.setting_value(setting, default=default)
        return values

    def _setting_enabled(
        self, setting: str | None, default: bool | str, enabled_value: bool | str
    ) -> bool:
        if setting is None:
            return False
        value = self.setting_value(setting, default=default)
        return type(value) is type(enabled_value) and value == enabled_value

    def fix_on_save(self, module: LintModule) -> bool:
        return self._setting_enabled(
            module.fix_on_save_setting,
            module.fix_on_save_default,
            module.fix_on_save_enabled_value,
        )

    def fix_existing(self, module: LintModule) -> bool:
        return module.maintenance_role == "trigger" and self.fix_on_save(module)

    def auto_approve(self, module: LintModule) -> bool:
        return self._setting_enabled(
            module.auto_approve_setting,
            module.auto_approve_default,
            module.auto_approve_enabled_value,
        )
