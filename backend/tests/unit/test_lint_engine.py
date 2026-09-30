import pytest

from backend.services.lint import base
from backend.services.lint.base import LintFindingDraft, enabled_rules, register_rule


class _Dummy:
    """Duck-typed LintRule for engine tests (no DB, no registration)."""

    def __init__(
        self,
        rule_id,
        *,
        layer="format",
        default_enabled=True,
        applies_to=frozenset({"context_page"}),
    ):
        self.rule_id = rule_id
        self.layer = layer
        self.default_enabled = default_enabled
        self.applies_to = applies_to

    def check(self, *, session, workspace_id, target):
        return [
            LintFindingDraft(
                rule_id=self.rule_id,
                severity="warning",
                target_type="context_page",
                target_id=None,
            )
        ]


def test_enabled_rules_respects_policy_and_defaults():
    rules = [_Dummy("t.a", default_enabled=True), _Dummy("t.b", default_enabled=False)]
    # No policy -> fall back to default_enabled.
    assert {r.rule_id for r in enabled_rules(None, rules=rules)} == {"t.a"}
    # Policy overrides both defaults.
    flipped = enabled_rules({"t.a": False, "t.b": True}, rules=rules)
    assert {r.rule_id for r in flipped} == {"t.b"}


def test_enabled_rules_filters_layer_and_target_type():
    rules = [
        _Dummy("t.fmt", layer="format"),
        _Dummy("t.kb", layer="knowledge"),
        _Dummy("t.lib", applies_to=frozenset({"library_item"})),
    ]
    assert {r.rule_id for r in enabled_rules(None, rules=rules, layer="knowledge")} == {
        "t.kb"
    }
    # t.lib only applies to library_item, so it is excluded for context_page.
    ctx = enabled_rules(None, rules=rules, target_type="context_page")
    assert {r.rule_id for r in ctx} == {"t.fmt", "t.kb"}


def test_register_rule_dedup_and_layer_validation():
    register_rule(_Dummy("t.unique.dedup"))
    try:
        with pytest.raises(ValueError, match="duplicate"):
            register_rule(_Dummy("t.unique.dedup"))
        with pytest.raises(ValueError, match="unknown lint layer"):
            register_rule(_Dummy("t.badlayer", layer="bogus"))
    finally:
        base._REGISTRY.pop("t.unique.dedup", None)


def test_register_rule_rejects_empty_applies_to_and_nonbool_default():
    # J2: an empty applies_to would register but never run; a non-bool
    # default_enabled is a typo — both fail fast at registration.
    with pytest.raises(ValueError, match="applies_to"):
        register_rule(_Dummy("t.empty", applies_to=frozenset()))
    with pytest.raises(ValueError, match="default_enabled"):
        register_rule(_Dummy("t.nonbool", default_enabled="yes"))
    assert "t.empty" not in base._REGISTRY
    assert "t.nonbool" not in base._REGISTRY
