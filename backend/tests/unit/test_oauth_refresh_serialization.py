import threading
import time
from concurrent.futures import ThreadPoolExecutor

from backend.services.oauth_service import OAuthService, OAuthTokenResult


def test_refresh_requests_enter_rotation_one_at_a_time(monkeypatch):
    """Two threads must not sit inside the lookup/rotate window together.

    The stand-in is patched onto the CLASS, not the instance: OAuthService is a
    frozen dataclass, so assigning to an instance attribute raises.
    """

    service = object.__new__(OAuthService)
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_locked(
        self,
        *,
        refresh_token: str,
        client_id: str,
        requested_scope: str | None = None,
    ) -> OAuthTokenResult:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with state_lock:
            active -= 1
        return OAuthTokenResult("access", refresh_token, 60, client_id)

    monkeypatch.setattr(OAuthService, "_refresh_access_token_locked", fake_locked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda token: service.refresh_access_token(
                    refresh_token=token, client_id="client"
                ),
                ("refresh-1", "refresh-2"),
            )
        )

    assert [result.refresh_token for result in results] == ["refresh-1", "refresh-2"]
    assert max_active == 1
