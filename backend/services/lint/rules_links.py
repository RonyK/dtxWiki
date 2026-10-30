"""Body rules and fixer share the authored-link decision table."""

from __future__ import annotations

from backend.services.context_links import (
    LinkContext,
    configured_self_origins,
    decide_link,
    editor_link_actor,
    extract_links,
    link_decision_batch,
    rewrite_links_with_report,
)
from backend.services.lint.base import (
    FixResult,
    LintContext,
    LintFindingDraft,
    LintModule,
    LintStage,
)


def link_context(ctx: LintContext) -> LinkContext | None:
    if ctx.session is None or ctx.workspace_id is None:
        return None
    actor = ctx.actor
    # WriteContext carries authenticated write provenance, not an ActorContext.
    # The ingress already authorized it; retain identity for private own edits.
    if actor is not None and hasattr(actor, "actor_provenance"):
        actor = editor_link_actor(actor.actor_kind, actor.actor_id)
    return LinkContext(
        ctx.session,
        ctx.workspace_id,
        actor,
        getattr(ctx.page, "id", None),
        getattr(ctx.version, "id", None),
        ctx.version is not None
        and ctx.version.id == getattr(ctx.page, "current_published_version_id", None),
        ctx.self_origins if ctx.self_origins is not None else configured_self_origins(),
    )


def _decisions(body: str, ctx: LintContext):
    key = (
        "links",
        body,
        getattr(ctx.page, "id", None),
        getattr(ctx.version, "id", None),
    )
    if key not in ctx.body_analysis:
        context = link_context(ctx)
        if context is None:
            ctx.body_analysis[key] = []
        else:
            occurrences = extract_links(body)
            batch = link_decision_batch(occurrences, context)
            ctx.body_analysis[key] = [
                (locator, decision)
                for occurrence in occurrences
                for locator, _, decision in [
                    decide_link(occurrence.href, context, batch=batch)
                ]
            ]
    return ctx.body_analysis[key]


class _LinkRule:
    layer = "knowledge"
    default_enabled = True
    applies_to = frozenset({"context_page"})
    stages = frozenset({LintStage.SAVE, LintStage.READ, LintStage.RUN})

    def __init__(self, rule_id: str) -> None:
        self.rule_id = rule_id

    def check_body(self, body: str, ctx: LintContext):
        findings = []
        seen = set()
        for locator, decision in _decisions(body, ctx):
            if decision.finding != self.rule_id or locator.key in seen:
                continue
            if ctx.stage == LintStage.READ and self.rule_id == "link.unresolved":
                # A reader sees the same unavailable rendering for hidden and
                # missing destinations; reading never produces their findings.
                continue
            seen.add(locator.key)
            messages = {
                "link.unresolved": "This document link is unavailable.",
                "link.foreign": "Use a full permanent link for another workspace.",
                "link.target-unpublished": "The target has no published version.",
                "link.internal-noncanonical": "Use the permanent document link.",
            }
            findings.append(
                LintFindingDraft(
                    rule_id=self.rule_id,
                    severity=decision.severity,
                    target_type="context_page",
                    target_id=getattr(ctx.page, "id", None),
                    suggested_action=messages[self.rule_id],
                    message_key=self.rule_id,
                    message_args={
                        "locator_kind": locator.kind,
                        "locator": locator.href,
                        "written_slug": locator.slug or "",
                        **({"replacement": decision.href} if decision.href else {}),
                    },
                    fingerprint=f"{self.rule_id}|{locator.key}",
                )
            )
        return findings


class _LinkFixer:
    def fix(self, body: str, ctx: LintContext) -> FixResult:
        context = link_context(ctx)
        if context is None:
            return FixResult(body)
        rewritten, fixes, details = rewrite_links_with_report(body, context)
        return FixResult(rewritten, fixes, details)


MODULE = LintModule(
    "links",
    100,
    tuple(
        _LinkRule(code)
        for code in (
            "link.unresolved",
            "link.foreign",
            "link.internal-noncanonical",
            "link.target-unpublished",
        )
    ),
    fixer=_LinkFixer(),
    fix_on_save_setting="link_fix_mode",
    fix_existing_setting="link_fix_mode",
    auto_approve_setting="auto_approve_link_fixes",
)
