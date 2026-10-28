"""Container-owned lint registry with no process-wide registration state."""

from collections.abc import Iterable
from types import MappingProxyType

from backend.services.lint.base import (
    LINT_LAYERS,
    BodyRule,
    LintModule,
    LintPolicy,
    LintRule,
    LintStage,
    TargetRule,
)


class LintRegistry:
    def __init__(self, modules: Iterable[LintModule]) -> None:
        self.modules = tuple(modules)
        rules: dict[str, LintRule] = {}
        module_ids: set[str] = set()
        for module in self.modules:
            if module.module_id in module_ids:
                raise ValueError(f"duplicate lint module_id: {module.module_id}")
            module_ids.add(module.module_id)
            for rule in module.rules:
                if rule.layer not in LINT_LAYERS:
                    raise ValueError(
                        f"unknown lint layer {rule.layer!r} for {rule.rule_id}"
                    )
                if not isinstance(rule.default_enabled, bool):
                    raise ValueError(f"default_enabled must be bool for {rule.rule_id}")
                if not rule.applies_to:
                    raise ValueError(f"applies_to must be non-empty for {rule.rule_id}")
                stages = getattr(rule, "stages", None)
                if not isinstance(stages, frozenset) or any(
                    not isinstance(stage, LintStage) for stage in stages
                ):
                    raise ValueError(f"invalid lint stages for {rule.rule_id}")
                if not stages and getattr(rule, "runtime_checkable", True):
                    raise ValueError(
                        f"runtime stages cannot be empty for {rule.rule_id}"
                    )
                body_rule = isinstance(rule, BodyRule)
                target_rule = isinstance(rule, TargetRule)
                if (
                    stages
                    & {
                        LintStage.SAVE,
                        LintStage.READ,
                        LintStage.PUBLISH_FLOOR,
                    }
                    and not body_rule
                ):
                    raise ValueError(
                        f"body stages require check_body for {rule.rule_id}"
                    )
                if LintStage.RUN in stages and not (body_rule or target_rule):
                    raise ValueError(
                        f"RUN requires check_body or check_target for {rule.rule_id}"
                    )
                if body_rule and rule.applies_to != frozenset({"context_page"}):
                    raise ValueError(
                        f"body rules must target context_page for {rule.rule_id}"
                    )
                if hasattr(rule, "version_scoped") and (
                    not isinstance(rule.version_scoped, bool)
                    or not target_rule
                    or rule.applies_to != frozenset({"context_page"})
                ):
                    raise ValueError(f"invalid version scope for {rule.rule_id}")
                if rule.rule_id in rules:
                    raise ValueError(f"duplicate lint rule_id: {rule.rule_id}")
                rules[rule.rule_id] = rule
        self._rules = MappingProxyType(rules)

    @classmethod
    def builtin(cls) -> "LintRegistry":
        from backend.services.lint.modules import BUILTIN_LINT_MODULES

        return cls(BUILTIN_LINT_MODULES)

    def rules(self) -> tuple[LintRule, ...]:
        return tuple(self._rules.values())

    def rule(self, rule_id: str) -> LintRule | None:
        return self._rules.get(rule_id)

    def rule_ids(self) -> frozenset[str]:
        return frozenset(self._rules)

    def enabled(
        self,
        policy: LintPolicy | dict[str, bool] | None,
        *,
        layer: str | None = None,
        target_type: str | None = None,
    ) -> list[LintRule]:
        resolved_policy = (
            policy
            if isinstance(policy, LintPolicy)
            else LintPolicy.from_overrides(policy)
        )
        return [
            rule
            for rule in self._rules.values()
            if getattr(rule, "runtime_checkable", True)
            and (layer is None or rule.layer == layer)
            and (target_type is None or target_type in rule.applies_to)
            and (
                LintStage.PUBLISH_FLOOR in rule.stages
                or resolved_policy.rule_enabled(
                    rule.rule_id, default_enabled=rule.default_enabled
                )
            )
        ]

    def rules_for(
        self,
        stage: LintStage,
        policy: LintPolicy | dict[str, bool] | None,
        *,
        target_type: str | None = None,
    ) -> list[LintRule]:
        return [
            rule
            for rule in self.enabled(policy, target_type=target_type)
            if stage in rule.stages
        ]

    def fixers_for_save(
        self,
        policy: LintPolicy,
        *,
        preview: bool = False,
    ) -> list[LintModule]:
        """Select configured fixers in execution order; explicit preview runs all."""
        return [
            module
            for module in sorted(self.modules, key=lambda module: module.order)
            if module.fixer is not None and (preview or policy.fix_on_save(module))
        ]
