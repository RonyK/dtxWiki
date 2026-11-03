"""Body rules and fixer share the authored-link decision table."""

from __future__ import annotations

from backend.services.context_links import (
    LinkContext,
    citation_href_decision,
    citation_link_occurrences,
    configured_self_origins,
    decide_link,
    editor_link_actor,
    extract_links,
    link_decision_batch,
    normalize_index_refs_with_report,
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
    # Keep the scoped caller's read authority when supplied by the ingress.
    # Legacy internal writers retain identity for private own edits.
    if actor is not None and hasattr(actor, "actor_provenance"):
        actor = editor_link_actor(
            actor.actor_kind,
            actor.actor_id,
            workspace_id=ctx.workspace_id,
            actor_context=getattr(actor, "actor", None),
        )
    source = ctx.source_version if ctx.source_version is not None else ctx.version
    source_is_published = (
        ctx.link_source == "published"
        if ctx.link_source is not None
        else ctx.version is not None
        and ctx.version.id == getattr(ctx.page, "current_published_version_id", None)
    )
    return LinkContext(
        ctx.session,
        ctx.workspace_id,
        actor,
        getattr(ctx.page, "id", None),
        getattr(source, "id", None),
        source_is_published,
        ctx.self_origins if ctx.self_origins is not None else configured_self_origins(),
    )


def _decisions(body: str, ctx: LintContext):
    key = (
        "links",
        body,
        getattr(ctx.page, "id", None),
        getattr(ctx.version, "id", None),
        getattr(ctx.source_version, "id", None),
        ctx.link_source,
    )
    if key not in ctx.body_analysis:
        context = link_context(ctx)
        if context is None:
            ctx.body_analysis[key] = []
        else:
            occurrences = extract_links(body) + citation_link_occurrences(body)
            batch = link_decision_batch(occurrences, context)
            ctx.body_analysis[key] = [
                (locator, decision, occurrence.kind == "citation")
                for occurrence in occurrences
                for locator, _, decision in [
                    (
                        citation_href_decision
                        if occurrence.kind == "citation"
                        else decide_link
                    )(occurrence.href, context, batch=batch)
                ]
            ]
    return ctx.body_analysis[key]


# An address that names no citable document, or a marker without one, stays
# in the text and is not registered; this message says why.
_CITATION_UNRESOLVED = "link.unresolved_citation"


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
        for locator, decision, citation in _decisions(body, ctx):
            if decision.finding != self.rule_id or (citation, locator.key) in seen:
                continue
            if ctx.stage == LintStage.READ and self.rule_id == "link.unresolved":
                # A reader sees the same unavailable rendering for hidden and
                # missing destinations; reading never produces their findings.
                continue
            seen.add((citation, locator.key))
            unresolved_citation = citation and self.rule_id == "link.unresolved"
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
                    suggested_action=(
                        "Cite a document of this workspace by its address."
                        if unresolved_citation
                        else messages[self.rule_id]
                    ),
                    message_key=(
                        _CITATION_UNRESOLVED if unresolved_citation else self.rule_id
                    ),
                    message_args={
                        "locator_kind": locator.kind,
                        "locator": locator.href,
                        "written_slug": locator.slug or "",
                        **({"replacement": decision.href} if decision.href else {}),
                        **({"citation": True} if citation else {}),
                    },
                    fingerprint=(
                        f"{self.rule_id}|citation|{locator.key}"
                        if citation
                        else f"{self.rule_id}|{locator.key}"
                    ),
                )
            )
        if self.rule_id == "link.unresolved" and ctx.stage != LintStage.READ:
            from backend.services.citation_parser import (
                extract_inline_citations,
                incomplete_inline_citations,
                is_legacy_citation_key,
            )

            for occurrence in extract_inline_citations(body, strict=False):
                if not is_legacy_citation_key(occurrence.href):
                    continue
                findings.append(
                    LintFindingDraft(
                        rule_id=self.rule_id,
                        severity="warning",
                        target_type="context_page",
                        target_id=getattr(ctx.page, "id", None),
                        suggested_action=(
                            "Replace this legacy citation key with a permanent "
                            "document address."
                        ),
                        message_key=_CITATION_UNRESOLVED,
                        message_args={
                            "citation_legacy_key": True,
                            "citation": True,
                            "locator": occurrence.href,
                        },
                        fingerprint=f"citation.legacy-key|{occurrence.href}",
                    )
                )
            for occurrence in incomplete_inline_citations(body):
                findings.append(
                    LintFindingDraft(
                        rule_id=self.rule_id,
                        severity="warning",
                        target_type="context_page",
                        target_id=getattr(ctx.page, "id", None),
                        suggested_action="Complete the citation as @cite{address}.",
                        message_key=_CITATION_UNRESOLVED,
                        message_args={"citation": True, "locator": occurrence.href},
                        fingerprint=f"citation.incomplete|{occurrence.href}",
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


class _IndexRefNormalizer:
    def fix(self, body: str, ctx: LintContext) -> FixResult:
        context = link_context(ctx)
        if context is None:
            return FixResult(body)
        rewritten, fixes, details, _ = normalize_index_refs_with_report(body, context)
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
    fix_on_save_default="auto",
    fix_on_save_enabled_value="auto",
    auto_approve_setting="auto_approve_link_fixes",
    auto_approve_default=True,
    maintenance_role="trigger",
    fixable_rule_ids=frozenset({"link.internal-noncanonical"}),
    normalizer=_IndexRefNormalizer(),
)
