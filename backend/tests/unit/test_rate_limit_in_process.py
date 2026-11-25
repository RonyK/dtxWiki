"""Sprint_5 Phase_3 Task 3.6 — /oauth/token in-process rate-limit (ADR A12).

The Sprint_5 production deployment is a single backend process (Phase_4
systemd unit ``dtxwiki.service``, ``Type=simple``); the in-process token
buckets in ``backend.services.oauth_service`` are the authoritative
rate-limit state. These tests pin:

1. The per-IP threshold (120 req/min) — Sprint-4 R-F6-1 contract.
2. The per-client_id threshold (60 req/min) — Sprint-4 R-F6-1 contract.
3. Cross-client / cross-IP bucket independence (positive control paired
   with the negative thresholds per ADR A6).
4. The single-process invariant — checked against Phase_4's systemd
   unit file when present; otherwise skipped so Phase_3 stands alone
   (Phase_3 is committed before Phase_4 lands).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.services import oauth_service as oauth_service_module
from backend.services.oauth_service import (
    TOKEN_RATE_LIMIT_PER_CLIENT,
    TOKEN_RATE_LIMIT_PER_IP,
    OAuthRateLimitError,
    consume_token_slot,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_token_buckets():
    """The buckets live in module-global dicts; the project conftest's
    autouse fixture already clears them, but we double-clear here so
    these unit tests can run on their own without depending on the
    integration conftest."""

    oauth_service_module._TOKEN_IP_BUCKET.clear()
    oauth_service_module._TOKEN_CLIENT_BUCKET.clear()
    yield
    oauth_service_module._TOKEN_IP_BUCKET.clear()
    oauth_service_module._TOKEN_CLIENT_BUCKET.clear()


# ---------------------------------------------------------------------------
# Per-axis threshold pins (ADR A6 keyword + raise check)
# ---------------------------------------------------------------------------


def test_per_client_60_per_min():
    """A single client_id may consume 60 slots in a minute; the 61st
    call raises ``OAuthRateLimitError`` carrying the keyword pin
    ``rate limit exceeded`` so the failure mode is unambiguous."""

    assert TOKEN_RATE_LIMIT_PER_CLIENT == 60

    # Each call below uses a different source IP so the per-IP bucket
    # (120/min, 2x the per-client threshold) cannot trip first and mask
    # the per-client check we want to pin.
    for i in range(TOKEN_RATE_LIMIT_PER_CLIENT):
        consume_token_slot(
            client_ip=f"10.0.0.{i}",
            client_id="client-per-client-60",
        )
    with pytest.raises(OAuthRateLimitError) as excinfo:
        consume_token_slot(
            client_ip="10.0.0.99",
            client_id="client-per-client-60",
        )
    assert "rate limit exceeded" in str(excinfo.value)
    assert "client_id" in str(excinfo.value)


def test_per_ip_120_per_min():
    """A single source IP may consume 120 slots in a minute; the 121st
    call raises ``OAuthRateLimitError`` carrying the keyword pin
    ``rate limit exceeded``. Each call uses a distinct client_id so the
    per-client bucket cannot trip first."""

    assert TOKEN_RATE_LIMIT_PER_IP == 120

    for i in range(TOKEN_RATE_LIMIT_PER_IP):
        consume_token_slot(
            client_ip="203.0.113.7",
            client_id=f"client-per-ip-{i}",
        )
    with pytest.raises(OAuthRateLimitError) as excinfo:
        consume_token_slot(
            client_ip="203.0.113.7",
            client_id="client-per-ip-overflow",
        )
    assert "rate limit exceeded" in str(excinfo.value)
    assert "client IP" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Positive controls — paired with the negative thresholds above (ADR A6)
# ---------------------------------------------------------------------------


def test_two_clients_have_independent_buckets():
    """Positive control for the per-client threshold: client A may hit
    its 60/min ceiling without blocking client B. We use distinct IPs
    per call so the per-IP bucket is not the limiting factor."""

    client_a = "client-a-independent"
    client_b = "client-b-independent"

    # Saturate client A.
    for i in range(TOKEN_RATE_LIMIT_PER_CLIENT):
        consume_token_slot(client_ip=f"198.51.100.{i}", client_id=client_a)
    with pytest.raises(OAuthRateLimitError):
        consume_token_slot(client_ip="198.51.100.200", client_id=client_a)

    # Client B is untouched and must succeed.
    consume_token_slot(client_ip="198.51.100.201", client_id=client_b)


def test_two_ips_have_independent_buckets():
    """Positive control for the per-IP threshold: a saturated IP must
    not bleed into a different IP's bucket. Distinct client_ids per
    call so the per-client bucket is not the limiting factor."""

    ip_a = "192.0.2.10"
    ip_b = "192.0.2.20"

    for i in range(TOKEN_RATE_LIMIT_PER_IP):
        consume_token_slot(client_ip=ip_a, client_id=f"client-ip-iso-{i}")
    with pytest.raises(OAuthRateLimitError):
        consume_token_slot(client_ip=ip_a, client_id="client-ip-iso-overflow")

    # IP B is untouched.
    consume_token_slot(client_ip=ip_b, client_id="client-ip-iso-other")


# ---------------------------------------------------------------------------
# Sliding-window expiry — explicit time monkeypatch (no time.sleep)
# ---------------------------------------------------------------------------


def test_bucket_refills_after_60_seconds(monkeypatch):
    """The sliding-window prunes timestamps older than 60s on every
    call. After advancing ``time.monotonic`` by 61 seconds, a saturated
    client may consume slots again. Pins the implementation against a
    future "fixed-window" refactor that would reset on minute
    boundaries instead."""

    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(oauth_service_module.time, "monotonic", fake_monotonic)

    client_id = "client-refill"
    for i in range(TOKEN_RATE_LIMIT_PER_CLIENT):
        consume_token_slot(client_ip=f"10.1.0.{i}", client_id=client_id)
    with pytest.raises(OAuthRateLimitError):
        consume_token_slot(client_ip="10.1.0.99", client_id=client_id)

    # Advance past the sliding window.
    fake_now[0] += 61
    # The saturated client may now consume again.
    consume_token_slot(client_ip="10.1.0.100", client_id=client_id)


# ---------------------------------------------------------------------------
# Single-process invariant (ADR A12, paired with Phase_4 systemd unit)
# ---------------------------------------------------------------------------


_SYSTEMD_UNIT_PATH = Path("infra/prod/dtxwiki.service")


@pytest.mark.skipif(
    not _SYSTEMD_UNIT_PATH.exists(),
    reason=(
        "Phase_4 systemd unit not yet committed — skipped during Phase_3 "
        "single-Task development; re-runs on Gate B after Phase_4 lands"
    ),
)
def test_single_process_invariant_documented():
    """ADR A12 requires production deployment as a single process so
    the in-process bucket counters above are authoritative. Phase_4
    Task 4.4 commits the systemd unit; this test pins the unit to
    ``Type=simple`` + exactly one ``ExecStart=`` line so a future
    refactor that flips to multi-worker (uvicorn --workers N, gunicorn
    pre-fork) trips an explicit failure rather than silently doubling
    the rate-limit ceiling."""

    text = _SYSTEMD_UNIT_PATH.read_text(encoding="utf-8")
    type_lines = [
        line for line in text.splitlines() if line.strip().startswith("Type=")
    ]
    exec_start_lines = [
        line for line in text.splitlines()
        if line.strip().startswith("ExecStart=")
    ]
    assert type_lines == ["Type=simple"], (
        f"systemd unit must declare Type=simple, got {type_lines!r}"
    )
    assert len(exec_start_lines) == 1, (
        f"systemd unit must have exactly one ExecStart, got "
        f"{len(exec_start_lines)} lines: {exec_start_lines!r}"
    )


def test_rate_limit_module_docstring_pins_single_process_invariant():
    """Phase_3-internal positive control for the cross-Phase
    invariant test above: even before Phase_4's systemd unit lands,
    the rate-limit module's module-level documentation must declare
    the single-process commitment so a reviewer can see the contract
    without reading the ADR bundle."""

    source = Path(oauth_service_module.__file__).read_text(encoding="utf-8")
    assert "single-process invariant" in source.lower() or (
        "single*" in source.lower() and "process" in source.lower()
    ), "ADR A12 single-process invariant must be documented in oauth_service.py"
    assert "ADR A12" in source, (
        "ADR A12 reference missing from oauth_service.py rate-limit section"
    )
