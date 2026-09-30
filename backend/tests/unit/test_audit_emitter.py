from uuid import uuid4, uuid5

import pytest

from backend.audit.emitter import (
    OAUTH_AUDIT_ACTIONS,
    OAUTH_AUDIT_ACTOR_KINDS,
    OAUTH_AUDIT_EXTERNAL_ID_NAMESPACE,
    OAUTH_AUDIT_TARGET_CONTRACTS,
    emit_oauth_event,
    emit_page_event,
)

pytestmark = pytest.mark.unit

EXPECTED_ACTIONS = frozenset(
    {
        "oauth.client.registered",
        "oauth.refresh.refused",
        "oauth.refresh.reuse",
        # P10.3 T3 (section 2.3, ADR A5 amendment): a rotated refresh token
        # reused within the 30s grace window mints a new sibling pair instead
        # of the reuse-detection family revoke above.
        "oauth.refresh.grace_reissue",
        "oauth.consent.csrf",
        "oauth.revoke.success",
        "oauth.token.access_revoked",
    }
)


def _default_workspace_id(session):
    from sqlalchemy import select

    from backend.models.workspace import Workspace

    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None
    return workspace.id


def test_emit_oauth_event_workspace_id_none_accepted_for_dcr(session_factory):
    """Sprint_5 Phase_3 Task 3.2 (ADR A11): DCR's
    ``oauth.client.registered`` is a global event and persists with
    ``workspace_id=NULL``. The Phase_1 stub previously raised on ``None``;
    after Task 3.2 the helper passes the value through to the (now nullable)
    column.
    """

    with session_factory.begin() as session:
        event = emit_oauth_event(
            session,
            "oauth.client.registered",
            target_id="dtxw-client-abc",
            target_kind="oauth_client",
            workspace_id=None,
            actor_kind="system",
            actor_id="dcr",
        )
        assert event.workspace_id is None
        assert event.action == "oauth.client.registered"


def test_emit_oauth_event_retention_class_sensitive(session_factory):
    with session_factory.begin() as session:
        event = emit_oauth_event(
            session,
            "oauth.refresh.reuse",
            target_id=uuid4(),
            target_kind="oauth_refresh_token",
            workspace_id=_default_workspace_id(session),
            actor_kind="oauth_client",
            actor_id="client-1",
        )
        assert event.retention_class == "sensitive"


def test_emit_oauth_event_unknown_action_rejected(session_factory):
    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="action"):
            emit_oauth_event(
                session,
                "oauth.unknown",
                target_id=uuid4(),
                target_kind="oauth_refresh_token",
                workspace_id=_default_workspace_id(session),
                actor_kind="oauth_client",
                actor_id="client-1",
            )


def test_emit_oauth_event_action_enum_freeze():
    assert OAUTH_AUDIT_ACTIONS == EXPECTED_ACTIONS


def test_emit_oauth_event_actor_kind_audit_set_freeze(session_factory):
    assert OAUTH_AUDIT_ACTOR_KINDS == frozenset(
        {"system", "user_session", "oauth_client"}
    )
    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="actor_kind"):
            emit_oauth_event(
                session,
                "oauth.refresh.reuse",
                target_id=uuid4(),
                target_kind="oauth_refresh_token",
                workspace_id=_default_workspace_id(session),
                actor_kind="oauth_jwt",
                actor_id="client-1",
            )


def test_emit_oauth_event_str_target_id_for_oauth_client(session_factory):
    with session_factory.begin() as session:
        event = emit_oauth_event(
            session,
            "oauth.client.registered",
            target_id="dtxw-client-abc",
            target_kind="oauth_client",
            workspace_id=_default_workspace_id(session),
            actor_kind="system",
            actor_id="dcr",
            payload={"client_id": "dtxw-client-abc"},
        )
        assert event.target_id == uuid5(
            OAUTH_AUDIT_EXTERNAL_ID_NAMESPACE,
            "oauth_client:dtxw-client-abc",
        )
        assert event.payload["target_external_id"] == "dtxw-client-abc"


def test_emit_oauth_event_str_target_id_rejected_for_uuid_kind(session_factory):
    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="target_id"):
            emit_oauth_event(
                session,
                "oauth.refresh.reuse",
                target_id="not-a-uuid",
                target_kind="oauth_refresh_token",
                workspace_id=_default_workspace_id(session),
                actor_kind="oauth_client",
                actor_id="client-1",
            )


# --- Sprint_5 Phase_2 fix-round-3 (Bot-B4): per-action target_kind contract ---


def _valid_target_for(action: str):
    """Pick a (target_id, target_kind) pair that satisfies the contract.

    ``oauth_client`` uses a string external id (validated by
    ``_normalize_oauth_target_id``); all other target kinds require a UUID.
    """
    allowed = sorted(OAUTH_AUDIT_TARGET_CONTRACTS[action])
    target_kind = allowed[0]
    target_id = "dtxw-client-abc" if target_kind == "oauth_client" else uuid4()
    return target_id, target_kind


def _actor_for(action: str) -> tuple[str, str]:
    if action == "oauth.client.registered":
        return "system", "dcr"
    if action == "oauth.consent.csrf":
        return "user_session", "user-1"
    return "oauth_client", "client-1"


@pytest.mark.parametrize("action", sorted(OAUTH_AUDIT_ACTIONS))
def test_emit_oauth_event_target_kind_contract_positive(session_factory, action):
    """Each action accepts its allow-listed target_kind values."""
    target_id, target_kind = _valid_target_for(action)
    actor_kind, actor_id = _actor_for(action)
    with session_factory.begin() as session:
        event = emit_oauth_event(
            session,
            action,
            target_id=target_id,
            target_kind=target_kind,
            workspace_id=_default_workspace_id(session),
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        assert event.action == action


@pytest.mark.parametrize("action", sorted(OAUTH_AUDIT_ACTIONS))
def test_emit_oauth_event_target_kind_contract_negative(session_factory, action):
    """A target_kind belonging to a different action raises ValueError with
    the 'invalid for action' keyword pin (ADR A6 negative pair to the
    positive matrix above)."""
    allowed = OAUTH_AUDIT_TARGET_CONTRACTS[action]
    foreign_target_kind = next(
        kind
        for other_action, kinds in OAUTH_AUDIT_TARGET_CONTRACTS.items()
        if other_action != action
        for kind in kinds
        if kind not in allowed
    )
    target_id = "dtxw-other-abc" if foreign_target_kind == "oauth_client" else uuid4()
    actor_kind, actor_id = _actor_for(action)
    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="invalid for action"):
            emit_oauth_event(
                session,
                action,
                target_id=target_id,
                target_kind=foreign_target_kind,
                workspace_id=_default_workspace_id(session),
                actor_kind=actor_kind,
                actor_id=actor_id,
            )


def test_oauth_audit_target_contract_covers_all_actions():
    """Pin the contract dict's keyset to the action enum (positive control)."""
    assert set(OAUTH_AUDIT_TARGET_CONTRACTS) == set(OAUTH_AUDIT_ACTIONS)


def test_emit_page_event_unchanged(session_factory):
    with session_factory.begin() as session:
        target_id = uuid4()
        event = emit_page_event(
            session,
            workspace_id=_default_workspace_id(session),
            actor_kind="system",
            actor_id="phase1-test",
            action="page.created",
            target_kind="context_page",
            target_id=target_id,
            payload={"ok": True},
        )
        assert event.action == "page.created"
        assert event.target_id == target_id
        assert event.retention_class == "standard"
