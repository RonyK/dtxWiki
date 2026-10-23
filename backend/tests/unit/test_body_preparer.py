from types import SimpleNamespace
from uuid import uuid4

from backend.services.body_preparer import (
    BodyInput,
    BodyPreparer,
    PageMetadataInput,
    WriteContext,
)
from backend.services.lint.base import (
    LintFindingDraft,
    LintModule,
    LintPolicy,
    LintStage,
)
from backend.services.lint.registry import LintRegistry


def test_body_preparer_is_idempotent_and_honors_injected_modules_and_policy():
    workspace = SimpleNamespace(id=uuid4(), auto_fix_enabled=False, lint_policy={})
    page = SimpleNamespace(id=uuid4())
    version = SimpleNamespace(id=uuid4())
    session = object()
    ctx = WriteContext(
        workspace=workspace,
        actor_kind="human",
        actor_id="author",
        actor_provenance="browser_session",
        page=page,
        version=version,
        session=session,
    )
    preparer = BodyPreparer()
    raw = "# Body\n\n* item   \n"
    fixed = "# Body\n\n- item\n"
    metadata = PageMetadataInput(
        title="Prepared title",
        slug="prepared-page",
        page_type="guide",
        tags=["one"],
        aliases=["Alias"],
    )

    for enabled in (False, True):
        workspace.auto_fix_enabled = enabled
        once = preparer.prepare(BodyInput(raw, metadata=metadata), ctx)
        twice = preparer.prepare(BodyInput(once.body, metadata=metadata), ctx)
        assert twice.body == once.body
        assert twice.content_hash == once.content_hash
        assert twice.metadata == once.metadata
        assert once.body.endswith(fixed if enabled else raw)
        assert once.metadata.page_type == "guide"
        assert once.metadata.tags == ["one"]
        assert once.metadata.aliases == ["Alias"]

    workspace.auto_fix_enabled = False
    historical = "---\ntitle: Old\ncustom: keep\n---\n" + raw
    assert preparer.prepare(BodyInput(historical), ctx).body == historical
    assert preparer.preview(raw, ctx).body == fixed
    # Explicit formatting also accepts an unsaved image placeholder, as the
    # pre-extraction endpoint did; save-time media policy is a different gate.
    placeholder = "![upload](blob:https://example.test/temporary)\n"
    assert preparer.preview(placeholder, ctx).body == placeholder

    observed = []

    class FinalBodyRule:
        rule_id = "test.final_body"
        layer = "format"
        default_enabled = True
        applies_to = frozenset({"context_page"})
        stages = frozenset({LintStage.SAVE})

        def check_body(self, body, lint_ctx):
            observed.append((body, lint_ctx))
            return [
                LintFindingDraft(
                    rule_id=self.rule_id,
                    severity="info",
                    target_type="context_page",
                    target_id=lint_ctx.page.id,
                )
            ]

    class EnabledPolicy(LintPolicy):
        def fix_on_save(self, module):
            return True

    def canonicalize_link(body, _ctx):
        return body.replace("draft-link", "canonical-link")

    def format_list(body, _ctx):
        return body.replace("* canonical-link", "- canonical-link")

    # Deliberately reverse registration order. The later formatter can only
    # produce the expected body after the earlier link module has run.
    registry = LintRegistry(
        (
            LintModule("format", 900, (FinalBodyRule(),), fixer=format_list),
            LintModule("links", 100, (), fixer=canonicalize_link),
        )
    )
    injected = BodyPreparer(registry=registry, policy_factory=EnabledPolicy)
    result = injected.prepare(BodyInput("# Body\n\n* draft-link\n"), ctx)
    assert result.body == "# Body\n\n- canonical-link\n"
    assert result.fixes == ("links", "format")
    assert [finding.rule_id for finding in result.findings] == ["test.final_body"]
    assert result.findings[0].target_id == page.id
    assert len(observed) == 1
    body_seen, lint_ctx = observed[0]
    assert body_seen == result.body
    assert lint_ctx.session is session
    assert lint_ctx.workspace_id == workspace.id
    assert lint_ctx.actor is ctx
    assert lint_ctx.page is page
    assert lint_ctx.version is version
