from types import SimpleNamespace

from backend.services.review_risk_policy import (
    ReviewRiskResolver,
    apply_review_preset,
    auto_approve_by_risk_entries,
    auto_approve_risk_levels,
    default_review_policy,
    normalized_review_policy,
    validate_review_policy,
)


def _workspace(policy=None):
    return SimpleNamespace(review_policy=policy or default_review_policy())


def test_balanced_resolver_preserves_current_context_write_risk_ladder():
    resolver = ReviewRiskResolver()

    create = resolver.resolve(
        workspace=_workspace(),
        event="context_draft_create",
        signals={"review_required": True},
    )
    warning = resolver.resolve(
        workspace=_workspace(),
        event="context_draft_create",
        signals={"citation_warning": True, "review_required": True},
    )
    rewrite = resolver.resolve(
        workspace=_workspace(),
        event="context_full_rewrite",
        signals={"review_required": True},
    )
    partial = resolver.resolve(
        workspace=_workspace(),
        event="context_partial_update",
        signals={"review_required": True},
    )

    assert create.risk_level == "low"
    assert warning.risk_level == "high"
    assert rewrite.risk_level == "medium"
    assert partial.risk_level == "low"
    assert "citation warning" in warning.summary
    assert warning.policy_snapshot["preset"] == "balanced"


def test_balanced_keeps_routine_context_writes_low_for_auto_approve_policy():
    policy = default_review_policy()

    assert policy["defaults"]["context_draft_create"] == "low"
    assert policy["defaults"]["context_partial_update"] == "low"
    assert policy["defaults"]["context_full_rewrite"] == "medium"
    # Codex #16: the balanced DEFAULT must not auto-approve any risk level until
    # an admin opts in (high was already manual; low/medium now disabled too).
    assert auto_approve_risk_levels(policy) == []
    by_risk = policy["automation"]["auto_approve_by_risk"]
    assert by_risk["low"] == {"allowed": False, "delay_days": 7}
    assert by_risk["medium"] == {"allowed": False, "delay_days": 14}
    assert by_risk["high"] == {"allowed": False, "delay_days": 28}
    assert auto_approve_by_risk_entries(policy) == []


def test_resolver_review_required_reflects_policy_not_input_signal():
    """F2 guard: the resolved review_required must be driven by policy
    resolution, not just the raw input signal. An input of review_required=False
    still flips to True when the policy-resolved risk meets the review threshold,
    and stays False when it does not — which is what the ingress audit payload
    records."""
    resolver = ReviewRiskResolver()

    escalated_policy = default_review_policy()
    escalated_policy["defaults"]["context_full_rewrite"] = "high"
    escalated_policy["review_required"]["from_risk"] = "high"
    escalated = resolver.resolve(
        workspace=_workspace(escalated_policy),
        event="context_full_rewrite",
        signals={"review_required": False},
    )
    assert escalated.risk_level == "high"
    # Input said no review, but the high-risk policy forces it on.
    assert escalated.review_required is True

    relaxed_policy = default_review_policy()
    relaxed_policy["defaults"]["context_draft_create"] = "low"
    relaxed_policy["review_required"]["from_risk"] = "high"
    relaxed = resolver.resolve(
        workspace=_workspace(relaxed_policy),
        event="context_draft_create",
        signals={"review_required": False},
    )
    assert relaxed.risk_level == "low"
    # Low risk + same false input stays not-required, proving the True above
    # came from policy resolution rather than the input signal.
    assert relaxed.review_required is False


def test_balanced_resolver_preserves_agent_and_audit_risk_ladder():
    resolver = ReviewRiskResolver()

    assert (
        resolver.resolve(
            workspace=_workspace(),
            event="agent_sensitive_draft",
            signals={"sensitive_agent_category": True},
        ).risk_level
        == "high"
    )
    assert (
        resolver.resolve(workspace=_workspace(), event="inline_comment").risk_level
        == "low"
    )
    assert (
        resolver.resolve(workspace=_workspace(), event="stale_alternative").risk_level
        == "medium"
    )


def test_review_policy_validation_rejects_unsafe_auto_approve_threshold():
    policy = default_review_policy()
    policy["review_required"]["from_risk"] = "low"
    policy["automation"]["auto_approve_by_risk"]["medium"]["allowed"] = True

    try:
        validate_review_policy(policy)
    except ValueError as exc:
        assert "auto_approve_by_risk.medium.allowed" in str(exc)
    else:
        raise AssertionError("unsafe auto approve policy was accepted")


def test_apply_review_preset_preserves_automation_choices():
    policy = default_review_policy()
    policy["automation"]["auto_approve_by_risk"]["low"]["allowed"] = True
    policy["automation"]["auto_approve_by_risk"]["low"]["delay_days"] = 3

    next_policy = apply_review_preset(policy, "fast_moving")

    assert next_policy["preset"] == "fast_moving"
    assert next_policy["automation"]["auto_approve_by_risk"]["low"]["allowed"] is True
    assert next_policy["automation"]["auto_approve_by_risk"]["low"]["delay_days"] == 3


def test_review_policy_converts_legacy_auto_approve_to_by_risk():
    policy = default_review_policy()
    policy["automation"].pop("auto_approve_by_risk")
    policy["automation"]["auto_approve_enabled"] = True
    policy["automation"]["auto_approve_below"] = "medium"
    policy["automation"]["auto_approve_after_hours"] = 168

    normalized = normalized_review_policy(policy)

    assert normalized["automation"]["auto_approve_by_risk"] == {
        "low": {"allowed": True, "delay_days": 7},
        "medium": {"allowed": True, "delay_days": 7},
        "high": {"allowed": False, "delay_days": 7},
    }
    assert auto_approve_by_risk_entries(normalized) == [
        {"risk_level": "low", "delay_days": 7},
        {"risk_level": "medium", "delay_days": 7},
    ]


def test_review_policy_accepts_high_auto_approve_when_opted_in():
    # F-9: high auto-approval is now permitted — an admin opts in via the UI's
    # "danger noted" acknowledgement, regardless of review_required.from_risk.
    policy = default_review_policy()
    policy["automation"]["auto_approve_by_risk"]["high"]["allowed"] = True

    validated = validate_review_policy(policy)

    assert (
        validated["automation"]["auto_approve_by_risk"]["high"]["allowed"] is True
    )
    assert {"risk_level": "high"} in [
        {"risk_level": entry["risk_level"]}
        for entry in auto_approve_by_risk_entries(validated)
    ]


def test_review_policy_accepts_zero_delay_auto_approve():
    # F-9: delay_days=0 (no aging wait) is a valid configuration.
    policy = default_review_policy()
    policy["automation"]["auto_approve_by_risk"]["low"]["allowed"] = True
    policy["automation"]["auto_approve_by_risk"]["low"]["delay_days"] = 0

    validated = validate_review_policy(policy)

    assert (
        validated["automation"]["auto_approve_by_risk"]["low"]["delay_days"] == 0
    )


def test_human_checkbox_is_honored_but_ai_policy_gate_remains():
    resolver = ReviewRiskResolver()
    unchecked_signals = {
        "review_required": False,
        "decision_page_type": True,
        "low_placement_confidence": True,
    }

    human_unchecked = resolver.resolve(
        workspace=_workspace(),
        event="context_ingress_auto",
        signals=unchecked_signals,
        actor_provenance="browser_session",
    )
    human_checked = resolver.resolve(
        workspace=_workspace(),
        event="context_ingress_review_required",
        signals={"review_required": True, "caller_needs_review": True},
        actor_provenance="browser_session",
    )
    ai = resolver.resolve(
        workspace=_workspace(),
        event="context_ingress_auto",
        signals=unchecked_signals,
        actor_provenance="mcp_client",
    )

    assert human_unchecked.review_required is False
    assert human_checked.review_required is True
    assert human_checked.signals["human_review_required"] is True
    assert ai.review_required is True


def test_objective_lint_error_is_high_risk_but_not_a_human_review_request():
    """A person's malformed body is held from publication, not sent to review.

    This test used to assert the opposite. Filing the author's own unclosed
    fence as a pending review asked somebody to approve a typo -- and since any
    editor may decide a pending item, the author could approve it themselves and
    put the malformed body live. The floor is now enforced as a publication
    invariant in the write paths; the risk level still records how serious it is.
    """
    resolution = ReviewRiskResolver().resolve(
        workspace=_workspace(),
        event="direct_page_write_audit",
        signals={"lint_error": True},
        actor_provenance="browser_session",
    )

    assert resolution.review_required is False
    assert resolution.risk_level == "high"
    assert "author:human_direct_write" in resolution.reason_codes
    assert resolution.signals["human_review_required"] is True


# D-3 Task 2: `resolve`'s `floor_level` ratchet. context_write_service passes
# the risk the caller-declared update_mode would have carried as a floor
# under the measured event, so a small edit submitted as `full_rewrite` (or a
# whole-document replace submitted as a "small" mode) can't buy a lower
# severity than its declaration already implied.


def test_floor_level_raises_severity_while_reason_codes_stay_keyed_to_event():
    resolution = ReviewRiskResolver().resolve(
        workspace=_workspace(),
        event="context_partial_update",  # balanced default: low
        floor_level="medium",
        signals={"review_required": True},
    )

    assert resolution.risk_level == "medium"
    assert resolution.reason_codes == [
        "default:context_partial_update:low",
        "floor:medium",
    ]


def test_floor_level_never_lowers_a_default_that_already_outranks_it():
    resolution = ReviewRiskResolver().resolve(
        workspace=_workspace(),
        event="context_full_rewrite",  # balanced default: medium
        floor_level="low",
        signals={"review_required": True},
    )

    assert resolution.risk_level == "medium"
    assert not any(code.startswith("floor:") for code in resolution.reason_codes)


def test_floor_level_absent_or_matching_adds_no_reason_code():
    resolution = ReviewRiskResolver().resolve(
        workspace=_workspace(),
        event="context_partial_update",
        floor_level="low",  # equals the default; nothing to ratchet
        signals={"review_required": True},
    )

    assert resolution.risk_level == "low"
    assert resolution.reason_codes == ["default:context_partial_update:low"]


def test_floor_applied_before_signals_so_a_raise_still_composes_on_top():
    """Ordering matters: the floor must land BEFORE raise_one_level/force_high
    signal handling. Applied after, a naive floor would overwrite whatever
    the signal raised the level to and neuter the signal. Here the base
    default is "low", the floor alone would only reach "medium", and a
    raise_one_level signal (manual_citation) must still promote it one
    further step to "high" -- strictly above the floor by itself.
    """
    resolution = ReviewRiskResolver().resolve(
        workspace=_workspace(),
        event="context_partial_update",  # balanced default: low
        floor_level="medium",
        signals={"manual_citation": True, "review_required": True},
    )

    assert resolution.risk_level == "high"
    assert resolution.reason_codes == [
        "default:context_partial_update:low",
        "floor:medium",
        "raise:manual_citation",
    ]


def test_floor_ratchet_keeps_recorded_event_honest_under_an_inverted_policy():
    """An operator can invert the workspace's own defaults
    (`context_full_rewrite` cheaper than `context_partial_update`). A naive
    `max(measured, declared)` composition taken OVER those defaults would
    silently pick the declared side's row, so the audit trail would call a
    whole-document replace a "partial update". Composing via an explicit
    floor keeps `default:<event>:<level>` keyed to the event that ACTUALLY
    happened (context_full_rewrite) while still landing on the safer,
    higher severity -- with a `floor:` reason code proving the ratchet fired.
    """
    inverted_policy = default_review_policy()
    inverted_policy["defaults"]["context_full_rewrite"] = "low"
    inverted_policy["defaults"]["context_partial_update"] = "medium"

    resolution = ReviewRiskResolver().resolve(
        workspace=_workspace(inverted_policy),
        event="context_full_rewrite",  # measured: what actually happened
        # what the declared mode (e.g. replace_section -> partial_update's
        # own default under this same inverted policy) would have cost
        floor_level="medium",
        signals={"review_required": True},
    )

    assert resolution.reason_codes[0] == "default:context_full_rewrite:low"
    assert "floor:medium" in resolution.reason_codes
    assert resolution.risk_level == "medium"
