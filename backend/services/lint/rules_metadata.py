from dataclasses import dataclass

from backend.services.lint.base import LintModule, LintStage


@dataclass(frozen=True)
class _ExternalRule:
    rule_id: str
    layer: str
    applies_to: frozenset[str]
    default_enabled: bool = True
    runtime_checkable: bool = False
    stages: frozenset[LintStage] = frozenset()
    # The suggested_action key naming the other Context page this finding is
    # about. The finding is meaningless without that page, so readers see it
    # only alongside it (see LintService._hidden_by_related_page).
    related_page_key: str | None = None

    def check_target(self, target, ctx):
        return []


MODULE = LintModule(
    "metadata",
    300,
    (
        _ExternalRule(
            "maintenance.nav_summary_suggestion",
            "maintenance",
            frozenset({"workspace_tree_node"}),
        ),
        _ExternalRule(
            "maintenance.coverage_gap_verdict",
            "maintenance",
            frozenset({"knowledge_coverage_gap"}),
        ),
        _ExternalRule(
            "maintenance.page_meta_suggestion",
            "maintenance",
            frozenset({"context_page"}),
        ),
        _ExternalRule(
            "maintenance.import_batch_classification",
            "maintenance",
            frozenset({"library_item"}),
        ),
        _ExternalRule(
            "maintenance.stale_contradiction_verdict",
            "maintenance",
            frozenset({"context_page"}),
        ),
        _ExternalRule(
            "maintenance.golden_query_candidates",
            "maintenance",
            frozenset({"workspace"}),
        ),
        _ExternalRule(
            "kb.hot_path_unmaterialized",
            "knowledge",
            frozenset({"context_page"}),
            related_page_key="related_page_id",
        ),
        _ExternalRule(
            "kb.semantic_near_duplicate",
            "knowledge",
            frozenset({"context_page"}),
            related_page_key="candidate_page_id",
        ),
        _ExternalRule("kb.misplaced_page", "knowledge", frozenset({"context_page"})),
        _ExternalRule(
            "kb.dependent_maybe_stale",
            "knowledge",
            frozenset({"context_page"}),
            related_page_key="published_page_id",
        ),
    ),
)
