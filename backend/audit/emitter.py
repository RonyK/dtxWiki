from typing import Any
from uuid import UUID, uuid5

from sqlalchemy.orm import Session

from backend.models.audit import AuditEvent
from backend.repositories.audit_repo import insert_audit_event

OAUTH_AUDIT_ACTIONS = frozenset(
    {
        "oauth.client.registered",
        "oauth.refresh.refused",
        "oauth.refresh.reuse",
        # P10.3 T3 (section 2.3, ADR A5 amendment): a rotated refresh token
        # reused within the 30s grace window mints a new sibling pair instead
        # of triggering the reuse-detection family revoke above.
        "oauth.refresh.grace_reissue",
        "oauth.consent.csrf",
        "oauth.revoke.success",
        "oauth.token.access_revoked",
    }
)
OAUTH_AUDIT_ACTOR_KINDS = frozenset({"system", "user_session", "oauth_client"})
OAUTH_AUDIT_EXTERNAL_ID_NAMESPACE = UUID("00000000-0000-0000-0000-000000000010")
OAUTH_AUDIT_STR_TARGET_KINDS = frozenset({"oauth_client"})

# Sprint_5 Phase_2 fix-round-3 (Bot-B4): per-action target_kind contract. An
# OAuth audit event's ``target_kind`` must be one of the allow-listed values
# for its ``action``. Previously the emitter validated ``action`` and
# ``actor_kind`` independently but accepted any ``target_kind``, which let
# callers conflate a refresh-token revocation with a client registration, for
# example. The contract below pins the Phase_3 schema agreement.
# Sprint_5 Phase_2 fix-round-4 (ARCH-FR3-1): audit-layer ``target_kind`` is
# the conceptual access token (``oauth_access_token``), not the storage-layer
# JTI table name. Phase_1 Overview §11.2 normalizes the audit naming to drop
# the ``_jti`` suffix; the SQL table ``oauth_access_token_jti`` stays as a
# storage implementation detail. Phase_3 Task 3.5 emit calls
# written per the plan would have ``ValueError``-ed against the old contract.
OAUTH_AUDIT_TARGET_CONTRACTS: dict[str, frozenset[str]] = {
    "oauth.client.registered": frozenset({"oauth_client"}),
    "oauth.refresh.refused": frozenset({"oauth_refresh_token"}),
    "oauth.refresh.reuse": frozenset({"oauth_refresh_token"}),
    "oauth.refresh.grace_reissue": frozenset({"oauth_refresh_token"}),
    "oauth.consent.csrf": frozenset({"oauth_consent_session"}),
    "oauth.revoke.success": frozenset({"oauth_refresh_token", "oauth_access_token"}),
    "oauth.token.access_revoked": frozenset({"oauth_access_token"}),
}


def emit_page_event(
    session: Session,
    workspace_id: UUID,
    actor_kind: str,
    actor_id: str,
    action: str,
    target_kind: str,
    target_id: UUID,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    return insert_audit_event(
        session=session,
        workspace_id=workspace_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload=payload,
    )


def emit_oauth_event(
    session: Session,
    action: str,
    *,
    target_id: UUID | str,
    target_kind: str,
    workspace_id: UUID | None = None,
    actor_kind: str,
    actor_id: str,
    payload: dict[str, Any] | None = None,
    retention_class: str = "sensitive",
) -> AuditEvent:
    """Single entry for OAuth-domain audit emits (Sprint_5 Phase_3 contract).

    Sprint_5 Phase_3 Task 3.2 (ADR A11): ``workspace_id`` may be ``None`` for
    global events that pre-date workspace binding. ``oauth.client.registered``
    (DCR) is currently the only such global action; all other OAuth audit
    actions still pass a concrete workspace_id. The audit schema keeps
    ``audit_event.workspace_id`` nullable so the helper can persist ``None``.
    """

    if action not in OAUTH_AUDIT_ACTIONS:
        raise ValueError("action not in OAUTH_AUDIT_ACTIONS")
    if actor_kind not in OAUTH_AUDIT_ACTOR_KINDS:
        raise ValueError("actor_kind not in OAUTH_AUDIT_ACTOR_KINDS")
    allowed_target_kinds = OAUTH_AUDIT_TARGET_CONTRACTS.get(action)
    # ``action`` was already validated against OAUTH_AUDIT_ACTIONS above so
    # ``allowed_target_kinds`` is guaranteed non-None; the lookup is just to
    # narrow the type for static checkers.
    if allowed_target_kinds is None or target_kind not in allowed_target_kinds:
        raise ValueError(
            f"target_kind {target_kind!r} is invalid for action {action!r}"
        )
    normalized_payload = dict(payload or {})
    normalized_target_id = _normalize_oauth_target_id(
        target_id=target_id,
        target_kind=target_kind,
        payload=normalized_payload,
    )
    return insert_audit_event(
        session=session,
        workspace_id=workspace_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        action=action,
        target_kind=target_kind,
        target_id=normalized_target_id,
        payload=normalized_payload,
        retention_class=retention_class,
    )


def _normalize_oauth_target_id(
    *,
    target_id: UUID | str,
    target_kind: str,
    payload: dict[str, Any],
) -> UUID:
    if isinstance(target_id, UUID):
        return target_id
    if target_kind not in OAUTH_AUDIT_STR_TARGET_KINDS:
        raise ValueError(f"target_id must be UUID for target_kind={target_kind}")
    payload.setdefault("target_external_id", target_id)
    return uuid5(OAUTH_AUDIT_EXTERNAL_ID_NAMESPACE, f"{target_kind}:{target_id}")
