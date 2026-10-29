"""/auth/login + /auth/signup in-process rate-limit (brute-force throttle).

Mirrors the /oauth/token sliding-window contract pinned in
``test_rate_limit_in_process.py`` (ADR A12 single-process invariant): the
buckets in ``backend.services.session_service`` are module-global, keyed by
client IP and normalized email, and prune a 60-second ``time.monotonic``
tail on every call. These tests pin:

1. The per-email threshold (10 attempts/min) — the online brute-force bound
   for a single account.
2. The per-IP threshold (120 attempts/min) — the credential-spray bound.
3. Cross-email / cross-IP bucket independence (positive controls per ADR A6).
4. Email normalization — case/whitespace variants share one bucket so an
   attacker cannot multiply the per-account budget.
5. Sliding-window refill after 60 seconds.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.services import session_service as session_service_module
from backend.services.session_service import (
    AUTH_RATE_LIMIT_PER_EMAIL,
    AUTH_RATE_LIMIT_PER_IP,
    AuthRateLimitError,
    consume_auth_slot,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_auth_buckets():
    """The buckets live in module-global dicts; the project conftest's
    autouse fixture already clears them, but we double-clear here so
    these unit tests can run on their own without depending on the
    integration conftest."""

    session_service_module._AUTH_IP_BUCKET.clear()
    session_service_module._AUTH_EMAIL_BUCKET.clear()
    yield
    session_service_module._AUTH_IP_BUCKET.clear()
    session_service_module._AUTH_EMAIL_BUCKET.clear()


# ---------------------------------------------------------------------------
# Per-axis threshold pins (ADR A6 keyword + raise check)
# ---------------------------------------------------------------------------


def test_per_email_10_per_min():
    """A single email may consume 10 slots in a minute; the 11th call
    raises ``AuthRateLimitError`` carrying the keyword pin ``rate limit
    exceeded`` so the failure mode is unambiguous."""

    assert AUTH_RATE_LIMIT_PER_EMAIL == 10

    # Each call below uses a different source IP so the per-IP bucket
    # (120/min, 12x the per-email threshold) cannot trip first and mask
    # the per-email check we want to pin.
    for i in range(AUTH_RATE_LIMIT_PER_EMAIL):
        consume_auth_slot(
            client_ip=f"10.0.0.{i}",
            email="per-email-10@example.com",
        )
    with pytest.raises(AuthRateLimitError) as excinfo:
        consume_auth_slot(
            client_ip="10.0.0.99",
            email="per-email-10@example.com",
        )
    assert "rate limit exceeded" in str(excinfo.value)
    assert "email" in str(excinfo.value)


def test_per_ip_120_per_min():
    """A single source IP may consume 120 slots in a minute; the 121st
    call raises ``AuthRateLimitError`` carrying the keyword pin ``rate
    limit exceeded``. Each call uses a distinct email so the per-email
    bucket cannot trip first."""

    assert AUTH_RATE_LIMIT_PER_IP == 120

    for i in range(AUTH_RATE_LIMIT_PER_IP):
        consume_auth_slot(
            client_ip="203.0.113.7",
            email=f"per-ip-{i}@example.com",
        )
    with pytest.raises(AuthRateLimitError) as excinfo:
        consume_auth_slot(
            client_ip="203.0.113.7",
            email="per-ip-overflow@example.com",
        )
    assert "rate limit exceeded" in str(excinfo.value)
    assert "client IP" in str(excinfo.value)
    # Independent failure mechanism: callback traffic from the same browser
    # must retain its own quota when native password attempts exhaust theirs.
    consume_auth_slot(
        client_ip="203.0.113.7",
        email="per-ip-overflow@example.com",
        namespace="sso_callback",
    )
    with pytest.raises(AuthRateLimitError):
        consume_auth_slot(client_ip="203.0.113.7", email="per-ip-overflow@example.com")


# ---------------------------------------------------------------------------
# Positive controls — paired with the negative thresholds above (ADR A6)
# ---------------------------------------------------------------------------


def test_two_emails_have_independent_buckets():
    """Positive control for the per-email threshold: email A may hit its
    10/min ceiling without blocking email B. We use distinct IPs per call
    so the per-IP bucket is not the limiting factor."""

    email_a = "independent-a@example.com"
    email_b = "independent-b@example.com"

    for i in range(AUTH_RATE_LIMIT_PER_EMAIL):
        consume_auth_slot(client_ip=f"198.51.100.{i}", email=email_a)
    with pytest.raises(AuthRateLimitError):
        consume_auth_slot(client_ip="198.51.100.200", email=email_a)

    # Email B is untouched and must succeed.
    consume_auth_slot(client_ip="198.51.100.201", email=email_b)


def test_two_ips_have_independent_buckets():
    """Positive control for the per-IP threshold: a saturated IP must not
    bleed into a different IP's bucket. Distinct emails per call so the
    per-email bucket is not the limiting factor."""

    ip_a = "192.0.2.10"
    ip_b = "192.0.2.20"

    for i in range(AUTH_RATE_LIMIT_PER_IP):
        consume_auth_slot(client_ip=ip_a, email=f"ip-iso-{i}@example.com")
    with pytest.raises(AuthRateLimitError):
        consume_auth_slot(client_ip=ip_a, email="ip-iso-overflow@example.com")

    # IP B is untouched.
    consume_auth_slot(client_ip=ip_b, email="ip-iso-other@example.com")


# ---------------------------------------------------------------------------
# Email normalization — variants must share a single bucket
# ---------------------------------------------------------------------------


def test_email_case_and_whitespace_variants_share_one_bucket():
    """``login`` lowercases and strips emails before the credential check,
    so the bucket must fold the same way — otherwise rotating the case of
    one address multiplies the per-account brute-force budget."""

    variants = [
        "victim@example.com",
        "Victim@Example.COM",
        " victim@example.com ",
        "VICTIM@EXAMPLE.COM",
    ]
    for i in range(AUTH_RATE_LIMIT_PER_EMAIL):
        consume_auth_slot(
            client_ip=f"172.16.0.{i}",
            email=variants[i % len(variants)],
        )
    with pytest.raises(AuthRateLimitError):
        consume_auth_slot(client_ip="172.16.0.99", email="  VicTim@example.com")


# ---------------------------------------------------------------------------
# Sliding-window expiry — explicit time monkeypatch (no time.sleep)
# ---------------------------------------------------------------------------


def test_bucket_refills_after_60_seconds(monkeypatch):
    """The sliding-window prunes timestamps older than 60s on every call.
    After advancing ``time.monotonic`` by 61 seconds, a saturated email may
    consume slots again. Pins the implementation against a future
    "fixed-window" refactor that would reset on minute boundaries
    instead."""

    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(session_service_module.time, "monotonic", fake_monotonic)

    email = "refill@example.com"
    for i in range(AUTH_RATE_LIMIT_PER_EMAIL):
        consume_auth_slot(client_ip=f"10.1.0.{i}", email=email)
    with pytest.raises(AuthRateLimitError):
        consume_auth_slot(client_ip="10.1.0.99", email=email)

    # Advance past the sliding window.
    fake_now[0] += 61
    # The saturated email may now consume again.
    consume_auth_slot(client_ip="10.1.0.100", email=email)


# ---------------------------------------------------------------------------
# Unbounded key growth — AUTH-07
# ---------------------------------------------------------------------------


def test_stale_bucket_keys_are_swept_after_the_window_drains(monkeypatch):
    """The per-key timestamp LISTS are already trimmed on every call, but the
    key COUNT used to grow without bound: an unauthenticated caller sending a
    different IP/email on every /auth/login or /auth/signup attempt
    accumulated one permanent dict entry per distinct value for the life of
    the process. The opportunistic sweep must drop keys whose window has
    fully drained once 60s has passed since the last sweep."""

    fake_now = [5000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(session_service_module.time, "monotonic", fake_monotonic)
    session_service_module._LAST_AUTH_BUCKET_SWEEP = 0.0

    distinct_key_count = 500
    for i in range(distinct_key_count):
        consume_auth_slot(client_ip=f"sweep-ip-{i}", email=f"sweep-{i}@example.com")

    assert len(session_service_module._AUTH_IP_BUCKET) == distinct_key_count
    assert len(session_service_module._AUTH_EMAIL_BUCKET) == distinct_key_count

    # Advance past the sliding window, then make one more call — the sweep
    # piggybacks on real traffic rather than a background thread.
    fake_now[0] += 61
    consume_auth_slot(client_ip="sweep-ip-trigger", email="sweep-trigger@example.com")

    # Every stale key was dropped; only the just-added key survives.
    assert len(session_service_module._AUTH_IP_BUCKET) == 1
    assert len(session_service_module._AUTH_EMAIL_BUCKET) == 1


def test_auth_bucket_updates_and_sweep_are_thread_safe(monkeypatch):
    monkeypatch.setattr(session_service_module.time, "monotonic", lambda: 120.0)
    session_service_module._LAST_AUTH_BUCKET_SWEEP = 0.0

    def consume(index: int) -> None:
        consume_auth_slot(
            client_ip=f"concurrent-ip-{index}",
            email=f"concurrent-{index}@example.com",
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(consume, range(200)))

    assert len(session_service_module._AUTH_IP_BUCKET) == 200
    assert len(session_service_module._AUTH_EMAIL_BUCKET) == 200
