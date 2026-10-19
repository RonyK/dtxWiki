"""Exact credential health must agree with SQL facets and cursor pages."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.api_token import ApiTokenWorkspace
from backend.models.base import utc_now
from backend.models.oauth import OAuthConsent, OAuthRefreshToken
from backend.models.user import User
from backend.models.workspace import MCPClient, WorkspaceMembership
from backend.services import settings_service
from backend.services.mcp_credential_health import load_credential_health
from backend.services.settings_service import SettingsService
from backend.tests.unit import test_mcp_access_db_consistency as fixtures
from backend.tests.unit.test_mcp_access_db_consistency import (
    _add_api_token,
    _human_actor,
    _oauth_client,
)

db = fixtures.db


def listing(db, status="all", limit=100, cursor=None, query=None):
    with db.factory() as control, db.factory() as workspace:
        return SettingsService().list_mcp_clients(
            control_session=control,
            workspace_session=workspace,
            workspace_id=db.workspace_id,
            actor=_human_actor(db),
            cursor_secret="health-test",
            status=status,
            limit=limit,
            cursor=cursor,
            query=query,
        )


@pytest.mark.parametrize("kind", ["api_token", "oauth_grant"])
def test_individual_credentials_and_filters_share_one_verdict(db, kind):
    now = utc_now()
    expected = {}
    with db.factory.begin() as session:
        if kind == "oauth_grant":
            session.add(_oauth_client("same-app", client_name="Same app"))
            session.flush()
            session.add(
                OAuthConsent(
                    user_id=db.user_id,
                    client_id="same-app",
                    workspace_id=db.workspace_id,
                    scope="mcp:read",
                )
            )
        for name, expiry, revoked, blocked in [
            ("valid", now + timedelta(days=10), False, False),
            ("expired", now - timedelta(days=1), False, False),
            ("revoked", now + timedelta(days=10), True, False),
            ("blocked", now + timedelta(days=10), False, True),
            ("blocked-expired", now - timedelta(days=1), False, True),
            ("fallback", now + timedelta(days=10), False, False),
        ]:
            if kind == "api_token":
                token = _add_api_token(session, db, name=name, lookup_hash=name)
                token.expires_at = expiry
                token.revoked_at = now if revoked else None
                token_id = token.id
                metadata = {"token_id": str(token_id), "token_kind": "api_token"}
            else:
                token_id = uuid4()
                token = OAuthRefreshToken(
                    user_id=db.user_id,
                    client_id="same-app",
                    grant_id=token_id,
                    token_lookup_hash=name,
                    scope="mcp:read",
                    expires_at=expiry,
                    revoked_at=now if revoked else None,
                )
                session.add(token)
                metadata = {
                    "token_id": str(token_id),
                    "token_kind": "oauth_jwt",
                    "oauth_client_id": "same-app",
                }
            session.add(
                MCPClient(
                    workspace_id=db.workspace_id,
                    name=name,
                    issued_to_user_id=db.user_id,
                    status="disabled" if blocked else "active",
                    runtime_binding_key=None
                    if name == "fallback"
                    else f"{kind}:{token_id}",
                    token_metadata=metadata,
                )
            )
            expected[name] = (
                "disabled"
                if blocked
                else "revoked"
                if revoked
                else "expired"
                if expiry <= now
                else "active"
            )
        session.add(
            MCPClient(
                workspace_id=db.workspace_id,
                name="mismatched-app",
                issued_to_user_id=db.user_id,
                runtime_binding_key=f"{kind}:{token_id}",
                token_metadata={"oauth_client_id": "wrong-app"},
            )
        )
        expected["mismatched-app"] = "unknown"
        if kind == "api_token":
            token = _add_api_token(
                session, db, name="indefinite", lookup_hash="indefinite"
            )
            session.add(
                MCPClient(
                    workspace_id=db.workspace_id,
                    name="indefinite",
                    issued_to_user_id=db.user_id,
                    runtime_binding_key=f"api_token:{token.id}",
                )
            )
            expected["indefinite"] = "active"
        else:
            # A rotated predecessor is revoked, but its own stable grant still
            # has a live successor. Another grant cannot supply that liveness.
            successor = OAuthRefreshToken(
                id=uuid4(),
                user_id=db.user_id,
                client_id="same-app",
                grant_id=uuid4(),
                token_lookup_hash="successor",
                scope="mcp:read",
                expires_at=now + timedelta(days=3),
            )
            parent = OAuthRefreshToken(
                user_id=db.user_id,
                client_id="same-app",
                grant_id=successor.grant_id,
                token_lookup_hash="parent",
                scope="mcp:read",
                expires_at=now + timedelta(days=1),
                revoked_at=now,
                rotated_to_id=successor.id,
            )
            session.add_all([parent, successor])
            session.add(
                MCPClient(
                    workspace_id=db.workspace_id,
                    name="rotated",
                    issued_to_user_id=db.user_id,
                    runtime_binding_key=f"oauth_grant:{parent.grant_id}",
                    token_metadata={"oauth_client_id": "same-app"},
                )
            )
            expected["rotated"] = "active"
        expected["Seed client"] = "unknown"
    page = listing(db)
    assert {row.name: row.connection_state for row in page.data} == expected
    for status in ["active", "disabled"]:
        rows = []
        cursor = None
        while True:
            page = listing(db, status, limit=2, cursor=cursor)
            rows.extend(page.data)
            cursor = page.next_cursor
            if cursor is None:
                break
        names = {
            name
            for name, state in expected.items()
            if (state == "active") == (status == "active")
        }
        assert {row.name for row in rows} == names
        assert len(rows) == len(names) == page.filtered_count
        assert page.status_counts.active + page.status_counts.disabled == len(expected)
    # Re-enabling only changes the admin toggle. Credential expiry persists.
    with db.factory.begin() as session:
        row = session.scalar(
            select(MCPClient).where(MCPClient.name == "blocked-expired")
        )
        row.status = "active"
    assert (
        next(
            r for r in listing(db).data if r.name == "blocked-expired"
        ).connection_state
        == "expired"
    )
    # A primary binding remains authoritative if only the issuer projection is
    # absent. A metadata-only legacy fallback without an issuer stays unknown.
    with db.factory.begin() as session:
        row = session.scalar(select(MCPClient).where(MCPClient.name == "valid"))
        row.issued_to_user_id = None
    assert (
        next(
            r for r in listing(db, "active").data if r.name == "valid"
        ).connection_state
        == "active"
    )
    with db.factory.begin() as session:
        row = session.scalar(select(MCPClient).where(MCPClient.name == "valid"))
        row.runtime_binding_key = None
    assert (
        next(r for r in listing(db).data if r.name == "valid").connection_state
        == "unknown"
    )


def test_cursor_retains_evaluation_time_but_a_new_listing_sees_expiry(db, monkeypatch):
    now = utc_now()
    with db.factory.begin() as session:
        for index in range(3):
            token = _add_api_token(
                session, db, name=f"clock-{index}", lookup_hash=f"clock-{index}"
            )
            token.expires_at = now + timedelta(seconds=1)
            session.add(
                MCPClient(
                    workspace_id=db.workspace_id,
                    name=f"clock-{index}",
                    issued_to_user_id=db.user_id,
                    runtime_binding_key=f"api_token:{token.id}",
                )
            )
    monkeypatch.setattr(settings_service, "utc_now", lambda: now)
    first = listing(db, "active", limit=2)
    assert first.filtered_count == 3
    monkeypatch.setattr(settings_service, "utc_now", lambda: now + timedelta(seconds=2))
    second = listing(db, "active", limit=2, cursor=first.next_cursor)
    assert second.filtered_count == 3
    assert len(second.data) == 1
    assert all(row.connection_state == "active" for row in second.data)
    assert listing(db, "active").filtered_count == 0
    # A cursor older than the freeze window re-evaluates at the current time.
    stale_at = now + settings_service.MCP_CLIENT_CURSOR_EVALUATION_MAX_AGE
    monkeypatch.setattr(
        settings_service, "utc_now", lambda: stale_at + timedelta(seconds=1)
    )
    stale = listing(db, "active", limit=2, cursor=first.next_cursor)
    assert stale.filtered_count == 0
    assert stale.data == []


def test_exact_binding_without_metadata_remains_searchable_by_app_name(db):
    now = utc_now()
    grant_id = uuid4()
    with db.factory.begin() as session:
        session.add(_oauth_client("exact-app", client_name="Discoverable Application"))
        session.flush()
        session.add(
            OAuthConsent(
                user_id=db.user_id,
                client_id="exact-app",
                workspace_id=db.workspace_id,
                scope="mcp:read",
            )
        )
        session.add(
            OAuthRefreshToken(
                user_id=db.user_id,
                client_id="exact-app",
                grant_id=grant_id,
                token_lookup_hash="searchable",
                scope="mcp:read",
                expires_at=now + timedelta(days=1),
            )
        )
        session.add(
            MCPClient(
                workspace_id=db.workspace_id,
                name="Runtime connection",
                issued_to_user_id=db.user_id,
                runtime_binding_key=f"oauth_grant:{grant_id}",
            )
        )
    page = listing(db, "active", query="discoverable")
    assert page.filtered_count == 1
    assert page.data[0].client_name == "Discoverable Application"


def test_lost_workspace_access_is_revoked_for_each_independent_cause(db):
    """Each credential has exactly one defect; the two controls stay active."""

    now = utc_now()
    with db.factory.begin() as session:
        outsider = User(email="out@example.com", password_hash="x", display_name="o")
        suspended = User(email="sus@example.com", password_hash="x", display_name="s")
        suspended.status = "disabled"
        session.add_all([outsider, suspended])
        session.flush()
        session.add(
            WorkspaceMembership(
                user_id=suspended.id, workspace_id=db.workspace_id, role="editor"
            )
        )
        for client_id in ("app-ok", "app-consent-removed"):
            session.add(_oauth_client(client_id, client_name=client_id))
        session.flush()

        def as_user(user_id):
            return SimpleNamespace(user_id=user_id, workspace_id=db.workspace_id)

        def pat(name, user_id=None, *, grant=True, grant_removed=False):
            owner = as_user(user_id or db.user_id)
            token = _add_api_token(session, owner, name=name, lookup_hash=name)
            link = session.scalar(
                select(ApiTokenWorkspace).where(ApiTokenWorkspace.token_id == token.id)
            )
            if not grant:
                session.delete(link)
            elif grant_removed:
                link.removed_at = now
            return owner.user_id, f"api_token:{token.id}"

        def oauth(name, user_id=None, client_id="app-ok", consent_removed=False):
            owner = user_id or db.user_id
            grant_id = uuid4()
            session.add(
                OAuthRefreshToken(
                    user_id=owner,
                    client_id=client_id,
                    grant_id=grant_id,
                    token_lookup_hash=name,
                    scope="mcp:read",
                    expires_at=now + timedelta(days=5),
                )
            )
            consent = session.get(
                OAuthConsent, (owner, client_id, db.workspace_id)
            ) or OAuthConsent(
                user_id=owner,
                client_id=client_id,
                workspace_id=db.workspace_id,
                scope="mcp:read",
            )
            consent.removed_at = now if consent_removed else None
            session.add(consent)
            return owner, f"oauth_grant:{grant_id}"

        expected = {
            "pat-ok": ("active", pat("pat-ok")),
            "pat-no-grant": ("revoked", pat("pat-no-grant", grant=False)),
            "pat-grant-removed": (
                "revoked",
                pat("pat-grant-removed", grant_removed=True),
            ),
            "pat-lost-membership": ("revoked", pat("pat-out", outsider.id)),
            "pat-inactive-user": ("revoked", pat("pat-sus", suspended.id)),
            "oauth-ok": ("active", oauth("oauth-ok")),
            "oauth-consent-removed": (
                "revoked",
                oauth("oc", client_id="app-consent-removed", consent_removed=True),
            ),
            "oauth-lost-membership": ("revoked", oauth("oauth-out", outsider.id)),
            "oauth-inactive-user": ("revoked", oauth("oauth-sus", suspended.id)),
        }
        for name, (_state, (user_id, binding)) in expected.items():
            session.add(
                MCPClient(
                    workspace_id=db.workspace_id,
                    name=name,
                    issued_to_user_id=user_id,
                    runtime_binding_key=binding,
                )
            )
    states = {name: state for name, (state, _key) in expected.items()}
    assert {r.name: r.connection_state for r in listing(db).data} == {
        **states,
        "Seed client": "unknown",
    }
    assert {r.name for r in listing(db, "active").data} == {"pat-ok", "oauth-ok"}


def test_loader_reads_only_the_named_credentials(db):
    with db.factory.begin() as session:
        tokens = [
            _add_api_token(session, db, name=f"t{i}", lookup_hash=f"t{i}")
            for i in range(3)
        ]
    named = f"api_token:{tokens[0].id}"
    with db.factory() as session:

        def load(*bindings):
            return load_credential_health(
                session, workspace_id=db.workspace_id, now=utc_now(), bindings=bindings
            )

        assert set(load(named, "api_token:not-a-uuid", "other:x", "oauth_grant:")) == {
            (db.user_id, named)
        }
        assert load() == {}


def test_policies_judge_only_the_page_bindings(db, monkeypatch):
    now = utc_now()
    with db.factory.begin() as session:
        for index in range(3):
            name = f"p{index}"
            token = _add_api_token(session, db, name=name, lookup_hash=name)
            session.add(
                MCPClient(
                    workspace_id=db.workspace_id,
                    name=f"p{index}",
                    issued_to_user_id=db.user_id,
                    runtime_binding_key=f"api_token:{token.id}",
                    last_used_at=now - timedelta(minutes=index),
                )
            )
    seen = {}
    real = settings_service.load_workspace_oauth_health

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(settings_service, "load_workspace_oauth_health", spy)
    with db.factory() as control, db.factory() as workspace:
        policies = SettingsService().get_policies(
            control_session=control,
            workspace_session=workspace,
            workspace_id=db.workspace_id,
            mcp_client_limit=2,
        )
    page = {row.name: row for row in policies.mcp_clients}
    assert set(page) == {"p0", "p1"}
    assert len(seen["bindings"]) == 2
    assert all(row.connection_state == "active" for row in page.values())
