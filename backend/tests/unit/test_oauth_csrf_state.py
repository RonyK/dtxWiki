from itertools import count

from backend.services import oauth_service


def test_oauth_csrf_state_buckets_are_bounded(monkeypatch):
    ticks = count()
    monkeypatch.setattr(
        oauth_service.time,
        "monotonic",
        lambda: float(next(ticks)) / 1000,
    )

    cap = oauth_service._OAUTH_CSRF_STATE_BUCKET_CAP

    for index in range(cap + 3):
        oauth_service.record_oauth_csrf_state_issued(f"issued-{index}")

    assert len(oauth_service._OAUTH_CSRF_ISSUED_STATES) == cap
    assert "issued-0" not in oauth_service._OAUTH_CSRF_ISSUED_STATES
    assert "issued-1" not in oauth_service._OAUTH_CSRF_ISSUED_STATES
    assert "issued-2" not in oauth_service._OAUTH_CSRF_ISSUED_STATES
    assert f"issued-{cap + 2}" in oauth_service._OAUTH_CSRF_ISSUED_STATES

    for index in range(cap + 2):
        oauth_service.consume_oauth_csrf_state(f"consumed-{index}")

    assert len(oauth_service._OAUTH_CSRF_CONSUMED_STATES) == cap
    assert "consumed-0" not in oauth_service._OAUTH_CSRF_CONSUMED_STATES
    assert "consumed-1" not in oauth_service._OAUTH_CSRF_CONSUMED_STATES
    assert f"consumed-{cap + 1}" in oauth_service._OAUTH_CSRF_CONSUMED_STATES
