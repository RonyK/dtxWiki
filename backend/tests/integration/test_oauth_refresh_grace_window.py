"""P10.3 T3 (section 2.3, ADR A5 amendment) -- the 30s refresh-reuse grace
window regression coverage (plan section 5, R4).

A rotated refresh token reused within 30 seconds of its OWN rotation mints a
new sibling pair instead of triggering reuse-detection family revoke (Auth0
"reuse interval" pattern). Reuse after the window, or when the first child
from that rotation was itself already explicitly revoked, still revokes the
whole family as before. See backend/services/oauth_service.py
(_refresh_access_token_locked) for the implementation and
REFRESH_REUSE_GRACE_WINDOW_SECONDS for the exact window length.

The service measures the window on its own clock, which these tests move by
assignment (``oauth_clock``), so every reuse lands at an exact distance from
the rotation and no host-clock margin is involved: a host clock step cannot
carry a reuse across the window's edge. The 30 seconds are pinned here, not
read from the constant, so that changing the window in the service fails the
tests instead of moving them along with it.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.oauth import OAuthRefreshToken
from backend.services.oauth_service import (
    _DCR_BUCKET,
    challenge_s256,
    token_hash,
)
from backend.tests.conftest import authenticate_test_client_in_workspace
from backend.tests.utils.oauth_clock import oauth_clock_fixture  # noqa: F401

# The reuse-grace window's documented length (P10.3 T3 section 2.3, ADR A5
# amendment). Pinned here, not read from ``REFRESH_REUSE_GRACE_WINDOW_SECONDS``,
# so that widening or narrowing the window in the service fails these tests
# instead of moving them along with it.
_GRACE_WINDOW = timedelta(seconds=30)
_ONE_MICROSECOND = timedelta(microseconds=1)


def _client(page_service) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
                mcp_http_mode="json_stateless",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(client, page_service.session_factory)
    return client


def _issue_token_pair(client: TestClient, *, state: str) -> dict[str, str]:
    """Walk DCR -> authorize (checklist, single workspace) -> token exchange."""

    # MIN_STATE_LENGTH is 22 chars -- pad short readable state labels out.
    state = (state + "-0123456789abcdef0123456789")[:22]
    _DCR_BUCKET.clear()
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": f"Grace window test {state}",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    workspace_response = client.get("/api/v1/workspaces")
    assert workspace_response.status_code == 200, workspace_response.text
    workspace_id = workspace_response.json()["data"]["current_workspace_id"]
    verifier = "v" * 64
    consent = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": state,
            "workspace_id": workspace_id,
        },
    )
    assert consent.status_code == 200, consent.text
    authorize = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": state,
            "workspace_ids": workspace_id,
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303, authorize.text
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    body = token.json()
    return {
        "client_id": client_id,
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
    }


def _refresh(client: TestClient, *, refresh_token: str, client_id: str):
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )


@pytest.mark.parametrize(
    "reuse_age",
    [timedelta(0), _GRACE_WINDOW - _ONE_MICROSECOND, _GRACE_WINDOW],
    ids=["at-rotation", "just-inside", "at-window"],
)
def test_reuse_within_grace_window_mints_new_pair_without_revoking_family(
    page_service, oauth_clock, reuse_age
):
    client = _client(page_service)
    issued = _issue_token_pair(client, state="grace-single-reuse")

    rotate = _refresh(
        client,
        refresh_token=issued["refresh_token"],
        client_id=issued["client_id"],
    )
    assert rotate.status_code == 200, rotate.text
    first_child_refresh = rotate.json()["refresh_token"]

    # Reuse the ORIGINAL (already-rotated) refresh token ``reuse_age`` after
    # its rotation -- from that very instant to the window's last, which is
    # inclusive -- and expect a NEW pair, not invalid_grant. The OAuth clock
    # has not moved since the rotation, so it still stands at that instant.
    oauth_clock.value += reuse_age
    reuse = _refresh(
        client,
        refresh_token=issued["refresh_token"],
        client_id=issued["client_id"],
    )
    assert reuse.status_code == 200, reuse.text
    grace_child_refresh = reuse.json()["refresh_token"]
    assert grace_child_refresh != first_child_refresh
    assert grace_child_refresh != issued["refresh_token"]

    # The first child (from the normal rotation) must remain fully live --
    # the grace window must not have revoked it.
    still_live = _refresh(
        client,
        refresh_token=first_child_refresh,
        client_id=issued["client_id"],
    )
    assert still_live.status_code == 200, still_live.text

    with page_service.session_factory() as session:
        parent = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(issued["refresh_token"])
            )
        )
        first_child_row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash == token_hash(first_child_refresh)
            )
        )
        grace_child_row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash == token_hash(grace_child_refresh)
            )
        )
        assert parent is not None and first_child_row is not None
        assert grace_child_row is not None
        # Parent's rotated_to_id keeps pointing at the FIRST child -- a grace
        # reissue never overwrites it.
        assert parent.rotated_to_id == first_child_row.id
        # Both children share the same rotated_from_id (the parent), so the
        # branch is reconstructable from the DB.
        assert first_child_row.rotated_from_id == parent.id
        assert grace_child_row.rotated_from_id == parent.id

        grace_event = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "oauth.refresh.grace_reissue")
        )
        assert grace_event is not None
        assert grace_event.payload["parent_refresh_id"] == str(parent.id)
        assert grace_event.payload["first_child_refresh_id"] == str(first_child_row.id)


def test_two_reuses_within_grace_window_both_succeed(page_service, oauth_clock):
    """No cap on how many times a reuse can happen within the window -- the
    window itself is the only limit (section 2.3)."""

    client = _client(page_service)
    issued = _issue_token_pair(client, state="grace-double-reuse")
    rotate = _refresh(
        client, refresh_token=issued["refresh_token"], client_id=issued["client_id"]
    )
    assert rotate.status_code == 200, rotate.text

    # Both reuses are measured from the one rotation: the first lands a
    # microsecond before the window's end, the second on its last instant.
    rotated_at = oauth_clock.value
    oauth_clock.value = rotated_at + _GRACE_WINDOW - _ONE_MICROSECOND
    first_reuse = _refresh(
        client, refresh_token=issued["refresh_token"], client_id=issued["client_id"]
    )
    assert first_reuse.status_code == 200, first_reuse.text
    oauth_clock.value = rotated_at + _GRACE_WINDOW
    second_reuse = _refresh(
        client, refresh_token=issued["refresh_token"], client_id=issued["client_id"]
    )
    assert second_reuse.status_code == 200, second_reuse.text
    assert first_reuse.json()["refresh_token"] != second_reuse.json()["refresh_token"]

    with page_service.session_factory() as session:
        parent = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(issued["refresh_token"])
            )
        )
        grace_children = list(
            session.scalars(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.rotated_from_id == parent.id
                )
            )
        )
        # First (normal) child + two grace-reissued siblings == 3 total.
        assert len(grace_children) == 3
        assert all(child.revoked_at is None for child in grace_children)


def test_reuse_after_first_child_explicitly_revoked_still_revokes_family(
    page_service, oauth_clock
):
    """Condition (c): an explicitly-revoked family must not be resurrected
    by a grace reuse, even within the 30s window."""

    client = _client(page_service)
    issued = _issue_token_pair(client, state="grace-revoked-child")
    rotate = _refresh(
        client, refresh_token=issued["refresh_token"], client_id=issued["client_id"]
    )
    assert rotate.status_code == 200, rotate.text
    first_child_refresh = rotate.json()["refresh_token"]

    # The revoke and the reuse both land on the window's last instant, which is
    # still inside it, so the revoked first child is the only thing left that
    # can refuse the reuse -- not an elapsed window.
    oauth_clock.value += _GRACE_WINDOW
    revoke = client.post(
        "/oauth/revoke",
        data={
            "token": first_child_refresh,
            "token_type_hint": "refresh_token",
            "client_id": issued["client_id"],
        },
    )
    assert revoke.status_code == 200, revoke.text

    reuse = _refresh(
        client, refresh_token=issued["refresh_token"], client_id=issued["client_id"]
    )
    assert reuse.status_code == 400, reuse.text
    assert reuse.json()["error"] == "invalid_grant"

    with page_service.session_factory() as session:
        parent = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(issued["refresh_token"])
            )
        )
        # No new sibling should have been minted.
        siblings = list(
            session.scalars(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.rotated_from_id == parent.id
                )
            )
        )
        assert len(siblings) == 1


def test_reuse_after_grace_window_expires_revokes_the_whole_family(
    page_service, oauth_clock
):
    """Reuse outside the window still triggers the existing family-revoke
    path, keyed by grant_id, so every branched child dies (section 2.3)."""

    client = _client(page_service)
    issued = _issue_token_pair(client, state="grace-window-expired")
    rotate = _refresh(
        client, refresh_token=issued["refresh_token"], client_id=issued["client_id"]
    )
    assert rotate.status_code == 200, rotate.text
    first_child_refresh = rotate.json()["refresh_token"]

    # Also mint a grace-reissued sibling before the window "expires" (halfway
    # through it), to confirm the eventual family revoke catches every
    # branched child.
    rotated_at = oauth_clock.value
    oauth_clock.value = rotated_at + timedelta(seconds=15)
    grace_reuse = _refresh(
        client, refresh_token=issued["refresh_token"], client_id=issued["client_id"]
    )
    assert grace_reuse.status_code == 200, grace_reuse.text
    grace_child_refresh = grace_reuse.json()["refresh_token"]

    # The window runs from the rotation itself, not from the sibling just
    # minted: one microsecond past its end the same token is a replay.
    oauth_clock.value = rotated_at + _GRACE_WINDOW + _ONE_MICROSECOND
    stale_reuse = _refresh(
        client, refresh_token=issued["refresh_token"], client_id=issued["client_id"]
    )
    assert stale_reuse.status_code == 400, stale_reuse.text
    assert stale_reuse.json()["error"] == "invalid_grant"

    with page_service.session_factory() as session:
        first_child_row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash == token_hash(first_child_refresh)
            )
        )
        grace_child_row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash == token_hash(grace_child_refresh)
            )
        )
        assert first_child_row.revoked_at is not None
        assert grace_child_row.revoked_at is not None
