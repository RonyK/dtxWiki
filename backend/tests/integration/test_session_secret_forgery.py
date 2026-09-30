"""A default local install no longer accepts sessions forged with public data.

Before 0.1.1 an unconfigured solo_local server signed its session cookies with
a constant published in the source tree, so anyone who learned a user id could
mint that user's session (readiness finding B-4). The secret is now generated
per installation.
"""

from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.user import User
from backend.services.session_service import SessionTokenCodec
from backend.tests.utils.environment import clear_ambient_dtxwiki_env

RETIRED_PUBLIC_SECRET = "dtxwiki-development-session-secret"


def _forged_session(page_service, user_id: str, secret: str) -> str:
    with page_service.session_factory() as session:
        user = session.get(User, UUID(user_id))
        assert user is not None
        token, _ = SessionTokenCodec(secret=secret).issue(user)
    return token


def test_cookie_signed_with_the_retired_default_secret_is_rejected(
    page_service, monkeypatch, tmp_path
):
    clear_ambient_dtxwiki_env(monkeypatch)
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "data"))
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")
    assert settings.app_mode == "solo_local"
    app = create_app(settings=settings, page_service=page_service)

    victim = TestClient(app)
    signup = victim.post(
        "/api/v1/auth/signup",
        json={"email": "victim@example.com", "password": "correct-password"},
    )
    assert signup.status_code in {200, 201}, signup.text
    user_id = victim.get("/api/v1/auth/me").json()["data"]["user_id"]

    cookie_name = settings.session_cookie_name
    attacker = TestClient(app)
    attacker.cookies.set(
        cookie_name, _forged_session(page_service, user_id, RETIRED_PUBLIC_SECRET)
    )
    forged = attacker.get("/api/v1/auth/me")
    assert forged.status_code == 401, forged.text

    # Positive control: the same construction with this installation's own
    # secret is accepted, so the 401 above is about the key, not the token.
    owner = TestClient(app)
    owner.cookies.set(
        cookie_name,
        _forged_session(page_service, user_id, settings.effective_session_secret),
    )
    genuine = owner.get("/api/v1/auth/me")
    assert genuine.status_code == 200, genuine.text
    assert genuine.json()["data"]["user_id"] == user_id
