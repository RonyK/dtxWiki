"""Poll ``/health/ready`` after a service (re)start.

Returns a bool (never raises) so update/rollback can decide what to do on a
failed start without the health probe itself becoming a failure mode.
"""

from __future__ import annotations

import time

import httpx

DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_INTERVAL_SEC = 1.0


def poll_health_ready(
    base_url: str,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
    request_timeout_sec: float = 5.0,
    sleep=time.sleep,
    now=time.monotonic,
) -> bool:
    """Return True once ``GET {base_url}/health/ready`` reports ``status=ready``.

    Polls until ``timeout_sec`` elapses. Any connection/HTTP error is treated as
    "not ready yet" and retried. ``sleep``/``now`` are injectable for tests.
    """

    url = base_url.rstrip("/") + "/health/ready"
    deadline = now() + timeout_sec
    while True:
        if _probe_ready(url, request_timeout_sec):
            return True
        if now() >= deadline:
            return False
        sleep(interval_sec)


def _probe_ready(url: str, request_timeout_sec: float) -> bool:
    try:
        response = httpx.get(url, timeout=request_timeout_sec)
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        return response.json().get("status") == "ready"
    except ValueError:
        return False
