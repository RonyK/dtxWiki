import pytest

from backend.services.lint import (
    LintFindingDraft,
    LintModule,
    LintPolicy,
    LintRegistry,
    LintStage,
)


class _Dummy:
    stages = frozenset({LintStage.RUN})
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

    def check_target(self, target, ctx):
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
    registry = LintRegistry((LintModule("test", 0, tuple(rules)),))
    # No policy -> fall back to default_enabled.
    assert {r.rule_id for r in registry.enabled(None)} == {"t.a"}
    # Policy overrides both defaults.
    flipped = registry.enabled({"t.a": False, "t.b": True})
    assert {r.rule_id for r in flipped} == {"t.b"}


def test_enabled_rules_filters_layer_and_target_type():
    rules = [
        _Dummy("t.fmt", layer="format"),
        _Dummy("t.kb", layer="knowledge"),
        _Dummy("t.lib", applies_to=frozenset({"library_item"})),
    ]
    registry = LintRegistry((LintModule("test", 0, tuple(rules)),))
    assert {r.rule_id for r in registry.enabled(None, layer="knowledge")} == {"t.kb"}
    # t.lib only applies to library_item, so it is excluded for context_page.
    ctx = registry.enabled(None, target_type="context_page")
    assert {r.rule_id for r in ctx} == {"t.fmt", "t.kb"}


def test_registry_dedup_and_layer_validation():
    with pytest.raises(ValueError, match="duplicate"):
        LintRegistry(
            (
                LintModule(
                    "test",
                    0,
                    (
                        _Dummy("t.unique.dedup"),
                        _Dummy("t.unique.dedup"),
                    ),
                ),
            )
        )
    with pytest.raises(ValueError, match="unknown lint layer"):
        LintRegistry((LintModule("test", 0, (_Dummy("t.badlayer", layer="bogus"),)),))


def test_registry_rejects_empty_applies_to_and_nonbool_default():
    # An empty target set would silently exclude a rule; a non-bool default is
    # a configuration typo. Both fail while the registry is assembled.
    with pytest.raises(ValueError, match="applies_to"):
        LintRegistry(
            (LintModule("test", 0, (_Dummy("t.empty", applies_to=frozenset()),)),)
        )
    with pytest.raises(ValueError, match="default_enabled"):
        LintRegistry(
            (LintModule("test", 0, (_Dummy("t.nonbool", default_enabled="yes"),)),)
        )
    invalid_stage = _Dummy("t.invalid_stage")
    invalid_stage.stages = frozenset({"unknown"})
    with pytest.raises(ValueError, match="invalid lint stages"):
        LintRegistry((LintModule("test", 0, (invalid_stage,)),))
    invalid_stage.stages = frozenset()
    with pytest.raises(ValueError, match="runtime stages cannot be empty"):
        LintRegistry((LintModule("test", 0, (invalid_stage,)),))
    target_only = _Dummy("t.target_only")
    target_only.stages = frozenset({LintStage.READ})
    with pytest.raises(ValueError, match="body stages require check_body"):
        LintRegistry((LintModule("test", 0, (target_only,)),))
    invalid_scope = _Dummy("t.invalid_scope")
    invalid_scope.version_scoped = "yes"
    with pytest.raises(ValueError, match="invalid version scope"):
        LintRegistry((LintModule("test", 0, (invalid_scope,)),))


def test_builtin_registry_and_container_module_injection_preserve_publication_floor():
    from types import SimpleNamespace

    from backend.app.container import assemble
    from backend.app.settings import Settings
    from backend.app.state import get_page_service_from_app
    from backend.mcp.context_tools import _context_edit_page_service

    builtin = LintRegistry.builtin()
    assert builtin is not LintRegistry.builtin()
    assert builtin.rule_ids() == {
        "md.heading_hierarchy",
        "md.code_fence_balance",
        "md.table_wellformed",
        "md.image_ref",
        "md.mermaid_block",
        "kb.orphan_page",
        "kb.missing_backlink",
        "kb.duplicate_concept",
        "kb.term_collision",
        "link.unresolved",
        "link.broken-slug",
        "maintenance.nav_summary_suggestion",
        "maintenance.coverage_gap_verdict",
        "maintenance.page_meta_suggestion",
        "maintenance.import_batch_classification",
        "maintenance.stale_contradiction_verdict",
        "maintenance.golden_query_candidates",
        "kb.hot_path_unmaterialized",
        "kb.semantic_near_duplicate",
        "kb.misplaced_page",
        "kb.dependent_maybe_stale",
        "nav.fanout_exceeded",
        "nav.missing_summary",
        "nav.unreachable",
    }
    extra = _Dummy("test.injected", default_enabled=False)
    container = assemble(
        Settings(),
        session_factory=SimpleNamespace(),
        lint_modules=(LintModule("test", 1000, (extra,)),),
    )
    services = container.get_services()
    registry = services.lint_registry
    assert registry.rule(extra.rule_id) is extra
    configured_modules = services.lint_modules
    with pytest.raises(ValueError, match="before the lint registry is constructed"):
        assemble(
            container.settings,
            services=services,
            lint_modules=(LintModule("late", 1100, (_Dummy("test.late"),)),),
        )
    assert services.lint_modules is configured_modules
    assert services.lint_registry is registry
    assert "test.late" not in registry.rule_ids()
    assert services.lint.lint_registry is registry
    assert services.settings_svc.lint_registry is registry
    assert services.domain_read.lint_registry is registry
    assert services.body_preparer.registry is registry
    pages = container.get_page_service()
    assert pages.body_preparer is services.body_preparer
    assert pages.change_audit_recorder.lint_registry is registry
    assert services.context_write.body_preparer is services.body_preparer
    assert services.context_ingress.body_preparer is services.body_preparer
    assert services.review.body_preparer is services.body_preparer
    mcp_pages = _context_edit_page_service(
        SimpleNamespace(
            session_factory=container.db.control_session_factory(),
            services=services,
        )
    )
    assert mcp_pages.body_preparer is services.body_preparer
    assert mcp_pages.lint_registry is registry
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(container=container))
    )
    assert get_page_service_from_app(request) is pages
    assert extra.rule_id not in builtin.rule_ids()
    policy = LintPolicy(SimpleNamespace(lint_policy={"md.code_fence_balance": False}))
    assert "md.code_fence_balance" in {
        rule.rule_id for rule in registry.enabled(policy)
    }
    assert extra.rule_id not in {rule.rule_id for rule in registry.enabled(policy)}
    assert {rule.rule_id for rule in registry.rules_for(LintStage.READ, policy)} == {
        "md.heading_hierarchy",
        "md.code_fence_balance",
        "md.table_wellformed",
        "md.image_ref",
        "md.mermaid_block",
    }
    assert [
        rule.rule_id for rule in registry.rules_for(LintStage.PUBLISH_FLOOR, policy)
    ] == [
        "md.code_fence_balance",
    ]
    catalogue = services.lint.list_rules(
        SimpleNamespace(
            get=lambda *_: SimpleNamespace(
                lint_policy={
                    "md.code_fence_balance": False,
                }
            )
        ),
        None,
    )
    floor_info = next(
        info for info in catalogue.rules if info.rule_id == "md.code_fence_balance"
    )
    assert floor_info.policy_locked is True and floor_info.enabled is True
    with pytest.raises(ValueError, match="duplicate lint rule_id"):
        LintRegistry(
            (*builtin.modules, LintModule("duplicate", 1, (builtin.rules()[0],)))
        )
