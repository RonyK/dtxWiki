"""The auto-approve delay accepted by a write must be the delay the form offers.

Settings > Review policy invites a delay of 0 ("no wait") — the field's minimum
is 0 and its helper text spells the meaning out — and the review policy contract
(``AUTO_APPROVE_DELAY_MIN_DAYS``) stores it. The settings write path disagreed
on its own: it rejected anything below 1, so saving the value the form offered
returned a 422, and the reader then clamped the stored 0 back up to 1. These
tests pin the three to one bound.
"""

import pytest

from backend.services.review_risk_policy import (
    AUTO_APPROVE_DELAY_MAX_DAYS,
    AUTO_APPROVE_DELAY_MIN_DAYS,
    RECONCILIATION_ATTENTION_MAX_DAYS,
    RECONCILIATION_REMINDER_MAX_HOURS,
)
from backend.services.settings_service import (
    SettingsPolicyUpdateRejected,
    _coerce_policy_value,
)

pytestmark = pytest.mark.unit

DELAY_KEY = "automation.auto_approve_by_risk.low.delay_days"


def test_zero_delay_is_accepted():
    assert _coerce_policy_value(DELAY_KEY, 0) == 0


def test_delay_bounds_follow_the_policy_contract():
    assert AUTO_APPROVE_DELAY_MIN_DAYS == 0
    assert _coerce_policy_value(DELAY_KEY, AUTO_APPROVE_DELAY_MIN_DAYS) == (
        AUTO_APPROVE_DELAY_MIN_DAYS
    )
    assert _coerce_policy_value(DELAY_KEY, AUTO_APPROVE_DELAY_MAX_DAYS) == (
        AUTO_APPROVE_DELAY_MAX_DAYS
    )


@pytest.mark.parametrize("value", [-1, AUTO_APPROVE_DELAY_MAX_DAYS + 1])
def test_delay_outside_the_contract_is_rejected(value):
    with pytest.raises(SettingsPolicyUpdateRejected):
        _coerce_policy_value(DELAY_KEY, value)


@pytest.mark.parametrize(
    ("key", "maximum"),
    [
        ("reconciliation.reminder_after_hours", RECONCILIATION_REMINDER_MAX_HOURS),
        ("reconciliation.attention_after_days", RECONCILIATION_ATTENTION_MAX_DAYS),
    ],
)
def test_reconciliation_operational_windows_follow_the_policy_contract(key, maximum):
    assert _coerce_policy_value(key, 1) == 1
    assert _coerce_policy_value(key, maximum) == maximum
    for invalid in (0, maximum + 1, True, "24"):
        with pytest.raises(SettingsPolicyUpdateRejected):
            _coerce_policy_value(key, invalid)
