"""MCP-11: the OAuth consents keyset must not drop rows when a column that
mutates during normal operation moves between page 1 and page 2.

Before this fix, ``list_user_consents`` paged on ``OAuthClient.last_used_at``
(touched on every token issuance/rotation). A row that gets touched between
fetching page 1 and page 2 can jump across the cursor boundary and vanish
from the union of both pages -- a connection silently disappears from the
operator's list.

``OAuthConsent.granted_at`` is not a safe alternative either: it is
overwritten on re-consent (oauth_service.py's authorize flow), so it has the
exact same failure mode. The fix pages on the immutable remainder of the
consent PK, ``(client_id, workspace_id)``, instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from backend.app.container import DatabaseProvider
from backend.app.settings import Settings
from backend.models.oauth import OAuthClient, OAuthConsent
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.services.oauth_service import OAuthService

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _oauth_service(session_factory) -> OAuthService:
    return OAuthService(
        session_factory=session_factory,
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        public_base_url="https://test.local",
        # Consent listing reads workspace-plane MCPClient rows.
        database_provider=DatabaseProvider(None, session_factory=session_factory),
    )


def _seed_user_and_workspace(session):
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None, "default workspace must be seeded"
    user = User(
        email="mcp11-pagination@example.com",
        password_hash="unused",
        display_name="MCP-11 Pagination",
        status="active",
    )
    session.add(user)
    session.flush()
    return user, workspace


def _seed_consents(session, *, user_id, workspace_id, count):
    for index in range(count):
        client_id = f"pagination-client-{index:02d}"
        session.add(
            OAuthClient(
                id=client_id,
                client_name=f"Client {index}",
                redirect_uris=["https://client.example/callback"],
                scope="mcp:read",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                last_used_at=BASE_TIME,
            )
        )
        session.add(
            OAuthConsent(
                user_id=user_id,
                client_id=client_id,
                workspace_id=workspace_id,
                scope="mcp:read",
                granted_at=BASE_TIME,
            )
        )
    session.flush()


def test_pagination_survives_last_used_at_touched_between_pages(session_factory):
    with session_factory.begin() as session:
        user, workspace = _seed_user_and_workspace(session)
        _seed_consents(session, user_id=user.id, workspace_id=workspace.id, count=3)
        user_id = user.id

    service = _oauth_service(session_factory)

    first_page = service.list_user_consents(user_id=user_id, limit=2)
    assert len(first_page["items"]) == 2
    assert first_page["next_cursor"] is not None

    # A refresh/token issuance for the client that would land on page 2
    # touches OAuthClient.last_used_at while the operator is mid-page --
    # this must not move that row across the cursor boundary.
    with session_factory.begin() as session:
        client = session.get(OAuthClient, "pagination-client-02")
        client.last_used_at = BASE_TIME + timedelta(days=30)

    second_page = service.list_user_consents(
        user_id=user_id,
        limit=2,
        cursor=first_page["next_cursor"],
    )

    seen_client_ids = {item["client_id"] for item in first_page["items"]} | {
        item["client_id"] for item in second_page["items"]
    }
    assert seen_client_ids == {
        "pagination-client-00",
        "pagination-client-01",
        "pagination-client-02",
    }


def test_pagination_survives_granted_at_overwritten_by_reconsent(session_factory):
    """granted_at is the OTHER mutable column a naive fix might reach for --
    pin that it is equally unsafe as a keyset column (it is overwritten on
    re-consent) so this seam does not regress to it later."""

    with session_factory.begin() as session:
        user, workspace = _seed_user_and_workspace(session)
        _seed_consents(session, user_id=user.id, workspace_id=workspace.id, count=3)
        user_id, workspace_id = user.id, workspace.id

    service = _oauth_service(session_factory)

    first_page = service.list_user_consents(user_id=user_id, limit=2)
    assert len(first_page["items"]) == 2
    assert first_page["next_cursor"] is not None

    # Simulate the re-consent path overwriting granted_at for the row that
    # would land on page 2 (oauth_service.py's authorize flow does exactly
    # this: consent.granted_at = utc_now()).
    with session_factory.begin() as session:
        consent = session.get(
            OAuthConsent, (user_id, "pagination-client-02", workspace_id)
        )
        consent.granted_at = BASE_TIME + timedelta(days=30)

    second_page = service.list_user_consents(
        user_id=user_id,
        limit=2,
        cursor=first_page["next_cursor"],
    )

    seen_client_ids = {item["client_id"] for item in first_page["items"]} | {
        item["client_id"] for item in second_page["items"]
    }
    assert seen_client_ids == {
        "pagination-client-00",
        "pagination-client-01",
        "pagination-client-02",
    }


def test_cursor_round_trip_is_stable_across_repeated_paging(session_factory):
    with session_factory.begin() as session:
        user, workspace = _seed_user_and_workspace(session)
        _seed_consents(session, user_id=user.id, workspace_id=workspace.id, count=5)
        user_id = user.id

    service = _oauth_service(session_factory)

    def _collect_all() -> list[str]:
        collected: list[str] = []
        cursor = None
        for _ in range(10):
            page = service.list_user_consents(user_id=user_id, limit=2, cursor=cursor)
            collected.extend(item["client_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        return collected

    first_pass = _collect_all()
    second_pass = _collect_all()
    assert first_pass == second_pass
    assert len(first_pass) == 5
