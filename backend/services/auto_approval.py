"""Auto-approval gate for agent context writes (plan D-T2/D-T3).

``needs_review`` is the CALLER's self-assessment, set via the MCP/REST write tool
by the agent inserting context. The objective vetoes are server-enforced so a
caller cannot bypass human review by leaving the flag false. The gate does not
replace dtxWiki's existing review-risk engine — it composes with it by
contributing force-review SIGNALS (it only ever ADDS review requirements):

    decision = evaluate_context_write(session, workspace_id=..., needs_review=...,
                                      body=...)
    signals = {**existing_signals, **decision.force_signals()}
    risk_resolution = risk_resolver.resolve(event=..., signals=signals)

Caller-requested review raises risk one level, while objective lint errors force
high risk. Both remain vetoes against direct and aged auto-approval.

Vetoes that need inputs the agent-write path does not carry (overwrite of an
already-published page, untrusted source trust tier) belong to the self-compile
ingest pipeline and are added there.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from backend.services.lint import LintRegistry
from backend.services.review_risk_policy import (
    GATE_FORCE_SIGNALS,
    GATE_RAISE_SIGNALS,
)


@dataclass(frozen=True)
class ReviewReason:
    """Why the gate forced human review, in terms the author can act on."""

    code: str
    message: str


@dataclass(frozen=True)
class AutoApprovalDecision:
    auto: bool
    vetoes: list[str]
    # The veto list alone is a set of opaque codes, so an agent was told its
    # draft "requires review" with no way to learn what to change. Carry the
    # underlying findings so the caller can be given the specific defect.
    reasons: tuple[ReviewReason, ...] = ()

    def force_signals(self) -> dict[str, bool]:
        """The risk-resolver signals this decision contributes (all True)."""
        return {
            veto: True
            for veto in self.vetoes
            if veto in GATE_FORCE_SIGNALS + GATE_RAISE_SIGNALS
        }


def evaluate_context_write(
    session: Session,
    *,
    workspace_id: UUID,
    needs_review: bool,
    body: str | None,
    lint_registry: LintRegistry | None = None,
) -> AutoApprovalDecision:
    """Decide whether an agent context write may auto-apply or must be reviewed.

    auto == True means "the gate does not force review" — the existing review
    policy still decides final auto-approval. auto == False means at least one
    veto fired and the write must go to human review.
    """
    vetoes: list[str] = []
    reasons: list[ReviewReason] = []
    if needs_review:
        vetoes.append("caller_needs_review")
        reasons.append(
            ReviewReason(
                code="caller_needs_review",
                message="The caller set needs_review, so this draft goes to review.",
            )
        )
    if body:
        from backend.services.lint import publish_floor_findings

        # Fixed objective floor only (unclosed fence/frontmatter) — see
        # publish_floor_findings. Cross-page wikilink checks are advisory (M6).
        findings = publish_floor_findings(body, registry=lint_registry)
        if findings:
            vetoes.append("lint_error")
            # The finding already carries the offending line number; passing it
            # through is the difference between "review required" and a defect
            # the author can actually go fix.
            reasons.extend(
                ReviewReason(
                    code=finding.rule_id,
                    message=finding.suggested_action or finding.rule_id,
                )
                for finding in findings
            )
    return AutoApprovalDecision(auto=not vetoes, vetoes=vetoes, reasons=tuple(reasons))
