from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.schemas.context_write import CitationInput, WriteDraftRequest
from backend.services.auto_approval import evaluate_context_write
from backend.services.context_write_service import ContextWriteService
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.review_risk_policy import (
    REVIEW_RISK_PRESETS,
    ReviewRiskResolver,
    apply_review_preset,
    default_review_policy,
)
from backend.services.review_service import ReviewService

pytestmark = pytest.mark.integration

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _ws(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default"))


def _citation(session, workspace_id, suffix, media_installer_factory):
    registration = LibraryService().register_library_item(
        session=session,
        workspace_id=workspace_id,
        canonical_uri=f"https://example.com/gate-src-{suffix}",
        title=f"Gate Src {suffix}",
        format_hint="markdown",
        raw_text="# Evidence\n\nVerified source text.",
    )
    ExtractionService(
        media_installer_factory=media_installer_factory
    ).extract_library_item_version(
        session=session, library_item_version_id=registration.version.id
    )
    return CitationInput(
        citation_type="raw_source",
        library_item_version_id=registration.version.id,
        snippet="Verified source text.",
    )


# --- D-T2: the gate decision service ---


def test_gate_needs_review_vetoes(session_factory):
    with session_factory.begin() as session:
        decision = evaluate_context_write(
            session, workspace_id=_ws(session).id, needs_review=True, body="# Ok\n\nx\n"
        )
    assert decision.auto is False
    assert "caller_needs_review" in decision.vetoes
    assert decision.force_signals() == {"caller_needs_review": True}


def test_gate_clean_write_is_auto(session_factory):
    with session_factory.begin() as session:
        decision = evaluate_context_write(
            session,
            workspace_id=_ws(session).id,
            needs_review=False,
            body="# Ok\n\nx\n",
        )
    assert decision.auto is True
    assert decision.vetoes == []
    assert decision.force_signals() == {}


def test_gate_lint_error_vetoes(session_factory):
    with session_factory.begin() as session:
        decision = evaluate_context_write(
            session,
            workspace_id=_ws(session).id,
            needs_review=False,
            body="```python\nunclosed fence\n",
        )
    assert decision.auto is False
    assert "lint_error" in decision.vetoes
    assert decision.force_signals() == {"lint_error": True}
    # The veto alone told the author nothing. Carry the defect through with the
    # line number so a caller can fix it instead of guessing.
    assert [reason.code for reason in decision.reasons] == ["md.code_fence_balance"]
    assert "line 1" in decision.reasons[0].message


def test_gate_unresolved_wikilink_does_not_veto(session_factory):
    # M6/DEC-2: cross-page wikilink resolution is advisory (forward-reference
    # false positives), so a link to a not-yet-created page must NOT gate the
    # write — only structural errors (unclosed fence/frontmatter) do.
    with session_factory.begin() as session:
        decision = evaluate_context_write(
            session,
            workspace_id=_ws(session).id,
            needs_review=False,
            body="See [[not-created-yet]] for details.\n",
        )
    assert decision.auto is True
    assert decision.vetoes == []


# --- D-T3: the resolver lever (gate signals adjust risk) ---


def test_resolver_raises_caller_review_and_forces_high_on_lint(session_factory):
    with session_factory.begin() as session:
        workspace = _ws(session)
        resolver = ReviewRiskResolver()
        base = resolver.resolve(
            workspace=workspace,
            event="context_draft_create",
            signals={"review_required": True},
        )
        forced = resolver.resolve(
            workspace=workspace,
            event="context_draft_create",
            signals={"review_required": True, "caller_needs_review": True},
        )
        lint = resolver.resolve(
            workspace=workspace,
            event="context_draft_create",
            signals={"review_required": True, "lint_error": True},
        )
    assert base.risk_level == "low"
    assert forced.risk_level == "medium"
    assert "raise:caller_needs_review" in forced.reason_codes
    assert lint.risk_level == "high"
    assert "force_high:lint_error" in lint.reason_codes


# --- D-T3: end-to-end through write_draft ---


def test_write_draft_needs_review_raises_risk_one_level(
    session_factory, media_installer_factory
):
    service = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _ws(session)
        control = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Control Page",
                page_type="overview",
                body_markdown="# Control\n\nA verified claim.",
                citations=[
                    _citation(session, workspace.id, "ctl", media_installer_factory)
                ],
                reason="control draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        control_review = session.get(ReviewItem, control.review_item_id)
        assert control_review.risk_level != "high"

        flagged = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Flagged Page",
                page_type="overview",
                body_markdown="# Flagged\n\nA verified claim.",
                citations=[
                    _citation(session, workspace.id, "flg", media_installer_factory)
                ],
                reason="flagged draft",
                needs_review=True,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        flagged_review = session.get(ReviewItem, flagged.review_item_id)
    assert flagged_review.risk_level == "medium"


@pytest.mark.parametrize("preset", REVIEW_RISK_PRESETS)
def test_needs_review_draft_is_never_auto_approved(
    session_factory, preset, media_installer_factory
):
    write_service = ContextWriteService()
    review_service = ReviewService()
    with session_factory.begin() as session:
        workspace = _ws(session)
        policy = apply_review_preset(default_review_policy(), preset)
        for level in ("low", "medium", "high"):
            policy["automation"]["auto_approve_by_risk"][level] = {
                "allowed": True,
                "delay_days": 0,
            }
        workspace.review_policy = policy
        result = write_service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title=f"Flagged Sweep Page {preset}",
                page_type="overview",
                body_markdown="# Flagged\n\nA verified claim.",
                citations=[
                    _citation(
                        session,
                        workspace.id,
                        f"sweep-{preset}",
                        media_installer_factory,
                    )
                ],
                reason="flagged sweep draft",
                needs_review=True,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        review = session.get(ReviewItem, result.review_item_id)
        review.created_at = datetime.now(UTC) - timedelta(days=365)
        session.flush()

        assert review.risk_signals["caller_needs_review"] is True
        assert review_service.count_auto_approve_candidates(session, workspace.id) == 0
        assert review_service.count_auto_approve_candidates_by_risk(
            session, workspace.id
        ) == {"low": 0, "medium": 0, "high": 0}
        assert (
            review_service.sweep_auto_approve_reviews(
                session, workspace.id, now=datetime.now(UTC)
            )
            == []
        )
        assert review.status == "pending"
