from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.catalog.mcp_apps import get_mcp_app_catalog_repository
from backend.models.api_token import ApiToken
from backend.models.workspace import MCPClient
from backend.policy.auth import ActorContext
from backend.services.audit_service import AuditService
from backend.services.db_write_intent import acquire_write_intent


class MCPClientRuntimeDenied(PermissionError):
    def __init__(self, *, error_code: str, client_id: UUID) -> None:
        message = {
            "mcp_client_disabled": (
                "This MCP client is disabled. A workspace owner must reactivate it."
            ),
            "mcp_client_deleted": (
                "This MCP client was deleted. Authorize a new credential to reconnect."
            ),
        }[error_code]
        super().__init__(message)
        self.error_code = error_code
        self.client_id = client_id


def runtime_binding_key_for_actor(actor: ActorContext) -> str:
    """Derive a stable, non-secret key for one authenticated credential.

    OAuth access-token rotation keeps ``token_id`` equal to the grant identity;
    PAT authentication supplies the token-row UUID. Consequently bearer
    rotation cannot create a fresh lifecycle row or bypass a tombstone.
    """

    if actor.actor_kind != "human" or actor.user_id is None:
        raise PermissionError(
            "unsupported_mcp_credential: MCP requires a PAT or OAuth bearer"
        )
    if actor.token_kind == "oauth_jwt":
        if actor.token_id is not None and actor.client_id:
            return f"oauth_grant:{actor.token_id}"
        raise PermissionError(
            "unsupported_mcp_credential: OAuth grant identity is incomplete"
        )
    if actor.token_kind in {"api_token", "pat"}:
        if actor.token_id is not None:
            return f"api_token:{actor.token_id}"
        raise PermissionError(
            "unsupported_mcp_credential: PAT identity is incomplete"
        )
    raise PermissionError(
        "unsupported_mcp_credential: MCP requires a PAT or OAuth bearer"
    )


def evaluate_mcp_client_runtime(
    session: Session,
    *,
    workspace_id: UUID,
    actor: ActorContext,
) -> MCPClientRuntimeDenied | None:
    """Register/bind a client and audit a lifecycle denial in this transaction.

    The caller must let the transaction commit before surfacing the returned
    denial. That preserves the denial audit without creating a tool trace, chat,
    or bundle mutation.
    """

    acquire_write_intent(session)
    client = resolve_trace_mcp_client(
        session,
        workspace_id=workspace_id,
        actor=actor,
        enforce_active=False,
    )
    denial = _runtime_denial(client)
    if denial is None:
        return None
    AuditService().emit(
        session=session,
        workspace_id=workspace_id,
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        action="mcp.client.runtime.denied",
        target_kind="mcp_client",
        target_id=client.id,
        payload={"reason": denial.error_code},
    )
    session.flush()
    return denial


def find_trace_mcp_client(
    session: Session,
    *,
    workspace_id: UUID,
    actor: ActorContext,
) -> MCPClient | None:
    """Resolve the same MCPClient ``resolve_trace_mcp_client`` would, without
    creating one.

    Conversation-ownership checks need the client identity a trace row WOULD be
    written under. Re-deriving it from the actor instead (actor_id / token_id)
    silently disagreed for OAuth and PAT actors, whose trace client is looked up
    by ``actor.client_id`` or synthesized from the token label — so every
    echoed conversation token came back ``conversation_token_unknown`` and the
    client got a brand new chat on each call.
    """

    runtime_binding_key = runtime_binding_key_for_actor(actor)
    bound_client = session.scalar(
        select(MCPClient).where(
            MCPClient.workspace_id == workspace_id,
            MCPClient.runtime_binding_key == runtime_binding_key,
        )
    )
    if bound_client is not None:
        return bound_client
    exact_client = _exact_external_actor_client(
        session,
        workspace_id=workspace_id,
        actor=actor,
        runtime_binding_key=runtime_binding_key,
    )
    if exact_client is not None:
        return exact_client
    name, _client_type, _brand_hint = _trace_client_identity(session, actor)
    client = session.scalar(
        _trace_name_lookup_statement(
            workspace_id=workspace_id,
            name=name,
            runtime_binding_key=runtime_binding_key,
        )
    )
    if client is not None:
        return client if _matches_external_actor(client, actor=actor) else None
    legacy_name = _legacy_oauth_trace_name(actor)
    if legacy_name is None:
        return None
    client = session.scalar(
        select(MCPClient).where(
            MCPClient.workspace_id == workspace_id,
            MCPClient.name == legacy_name,
            MCPClient.runtime_binding_key.is_(None),
        )
    )
    if client is not None and not _matches_external_actor(client, actor=actor):
        return None
    return client


def resolve_trace_mcp_client(
    session: Session,
    *,
    workspace_id: UUID,
    actor: ActorContext,
    enforce_active: bool = False,
) -> MCPClient:
    """Return an MCPClient row usable by durable MCP trace records."""

    runtime_binding_key = runtime_binding_key_for_actor(actor)
    client = session.scalar(
        select(MCPClient).where(
            MCPClient.workspace_id == workspace_id,
            MCPClient.runtime_binding_key == runtime_binding_key,
        )
    )
    if client is not None:
        _fill_missing_trace_fields(
            client,
            actor=actor,
            brand_hint=_brand_for(client.client_type),
            runtime_binding_key=runtime_binding_key,
        )
        if enforce_active:
            _raise_if_runtime_denied(client)
        return client

    # OAuth client identifiers are arbitrary strings (and may themselves be
    # UUID-shaped), while PAT identities are their full token UUIDs. Resolve
    # those durable external identities from token_metadata before consulting
    # the human-readable name. Besides being exact, this keeps pre-existing
    # rows -- including Settings-disabled legacy rows created under the old
    # truncated-name format -- authoritative after a naming-format upgrade.
    exact_client = _exact_external_actor_client(
        session,
        workspace_id=workspace_id,
        actor=actor,
        runtime_binding_key=runtime_binding_key,
    )
    if exact_client is not None:
        _fill_missing_trace_fields(
            exact_client,
            actor=actor,
            brand_hint=_brand_for(exact_client.client_type),
            runtime_binding_key=runtime_binding_key,
        )
        if enforce_active:
            _raise_if_runtime_denied(exact_client)
        return exact_client

    name, client_type, brand_hint = _trace_client_identity(session, actor)
    client = session.scalar(
        _trace_name_lookup_statement(
            workspace_id=workspace_id,
            name=name,
            runtime_binding_key=runtime_binding_key,
        )
    )
    if client is None:
        legacy_name = _legacy_oauth_trace_name(actor)
        if legacy_name is not None:
            client = session.scalar(
                select(MCPClient).where(
                    MCPClient.workspace_id == workspace_id,
                    MCPClient.name == legacy_name,
                    MCPClient.runtime_binding_key.is_(None),
                )
            )
    if client is not None:
        if not _matches_external_actor(client, actor=actor):
            raise RuntimeError(
                "MCP trace identity name is already bound to a different actor"
            )
        _fill_missing_trace_fields(
            client,
            actor=actor,
            brand_hint=brand_hint,
            runtime_binding_key=runtime_binding_key,
        )
        if enforce_active:
            _raise_if_runtime_denied(client)
        return client

    client = MCPClient(
        workspace_id=workspace_id,
        name=name,
        client_type=client_type,
        scopes=list(actor.scopes),
        status="active",
        created_by=actor.user_id,
        issued_to_user_id=actor.user_id,
        brand_hint=brand_hint,
        runtime_binding_key=runtime_binding_key,
        token_metadata={
            "token_kind": actor.token_kind,
            "token_id": str(actor.token_id) if actor.token_id else None,
            "oauth_client_id": actor.client_id,
        },
    )
    try:
        with session.begin_nested():
            session.add(client)
            session.flush()
    except IntegrityError as exc:
        # Concurrent first-touch already inserted this stable binding or its
        # collision-safe display name. Re-read so the losing trace converges on
        # the same binding. A name owned by a different binding is never an
        # identity fallback and therefore still fails closed.
        if client in session:
            session.expunge(client)
        client = session.scalar(
            select(MCPClient).where(
                MCPClient.workspace_id == workspace_id,
                MCPClient.runtime_binding_key == runtime_binding_key,
            )
        )
        if client is None:
            client = session.scalar(
                _trace_name_lookup_statement(
                    workspace_id=workspace_id,
                    name=name,
                    runtime_binding_key=runtime_binding_key,
                )
            )
        if client is None:
            raise
        if not _matches_external_actor(client, actor=actor):
            raise RuntimeError(
                "MCP trace identity name is already bound to a different actor"
            ) from exc
        _fill_missing_trace_fields(
            client,
            actor=actor,
            brand_hint=brand_hint,
            runtime_binding_key=runtime_binding_key,
        )
    if enforce_active:
        _raise_if_runtime_denied(client)
    return client


def _fill_missing_trace_fields(
    client: MCPClient,
    *,
    actor: ActorContext,
    brand_hint: str | None,
    runtime_binding_key: str,
) -> None:
    # Trace capture resolves an identity; it must not rewrite operator-managed
    # config on an existing row. status stays as set (a Settings-disabled row
    # is not resurrected by the next traced call) and scopes stay as issued —
    # only genuinely missing fields are filled in.
    if not client.scopes:
        client.scopes = list(actor.scopes)
    if client.issued_to_user_id is None:
        client.issued_to_user_id = actor.user_id
    if client.brand_hint is None:
        client.brand_hint = brand_hint
    if client.runtime_binding_key is None:
        client.runtime_binding_key = runtime_binding_key
    metadata = dict(client.token_metadata or {})
    missing_metadata = {
        "token_kind": actor.token_kind,
        "token_id": str(actor.token_id) if actor.token_id else None,
        "oauth_client_id": actor.client_id,
    }
    changed = False
    for key, value in missing_metadata.items():
        if key not in metadata and value is not None:
            metadata[key] = value
            changed = True
    if changed:
        client.token_metadata = metadata


def _runtime_denial(client: MCPClient) -> MCPClientRuntimeDenied | None:
    if client.deleted_at is not None:
        return MCPClientRuntimeDenied(
            error_code="mcp_client_deleted",
            client_id=client.id,
        )
    if client.status != "active":
        return MCPClientRuntimeDenied(
            error_code="mcp_client_disabled",
            client_id=client.id,
        )
    return None


def _raise_if_runtime_denied(client: MCPClient) -> None:
    denial = _runtime_denial(client)
    if denial is not None:
        raise denial


def _exact_external_actor_client(
    session: Session,
    *,
    workspace_id: UUID,
    actor: ActorContext,
    runtime_binding_key: str,
) -> MCPClient | None:
    candidates = session.scalars(
        select(MCPClient)
        .where(MCPClient.workspace_id == workspace_id)
        .order_by(MCPClient.created_at.asc(), MCPClient.id.asc())
    ).all()
    for candidate in candidates:
        if candidate.runtime_binding_key not in (
            None,
            runtime_binding_key,
        ):
            continue
        if not _matches_external_actor(candidate, actor=actor):
            continue
        return candidate
    return None


def _matches_external_actor(client: MCPClient, *, actor: ActorContext) -> bool:
    identity = _external_actor_identity(actor)
    metadata_key, metadata_value = identity
    if not _metadata_matches(
        client,
        metadata_key=metadata_key,
        metadata_value=metadata_value,
    ):
        return False
    if metadata_key == "oauth_client_id":
        if client.issued_to_user_id != actor.user_id:
            return False
        metadata = client.token_metadata or {}
        stored_token_id = (
            metadata.get("token_id") if isinstance(metadata, dict) else None
        )
        if actor.token_kind == "oauth_jwt" and actor.token_id is not None:
            return stored_token_id in (None, str(actor.token_id))
        return True
    return client.issued_to_user_id in (None, actor.user_id)


def _metadata_matches(
    client: MCPClient,
    *,
    metadata_key: str,
    metadata_value: str,
) -> bool:
    metadata = client.token_metadata or {}
    return isinstance(metadata, dict) and str(metadata.get(metadata_key) or "") == (
        metadata_value
    )


def _external_actor_identity(actor: ActorContext) -> tuple[str, str]:
    if actor.token_kind in {"api_token", "pat"} and actor.token_id is not None:
        return "token_id", str(actor.token_id)
    if actor.token_kind == "oauth_jwt" and actor.client_id:
        return "oauth_client_id", actor.client_id
    raise PermissionError(
        "unsupported_mcp_credential: MCP requires a PAT or OAuth bearer"
    )


def _trace_name_lookup_statement(
    *,
    workspace_id: UUID,
    name: str,
    runtime_binding_key: str,
):
    """Use a display name only to adopt an unbound legacy row.

    Stable bindings are the runtime identity. Once an actor has one, a row
    already bound to a different credential must never be selected merely
    because its human-readable name collides.
    """

    statement = select(MCPClient).where(
        MCPClient.workspace_id == workspace_id,
        MCPClient.name == name,
    )
    return statement.where(MCPClient.runtime_binding_key.is_(None))


def _trace_client_identity(
    session: Session,
    actor: ActorContext,
) -> tuple[str, str, str | None]:
    if actor.token_kind in {"api_token", "pat"} and actor.token_id is not None:
        token = session.get(ApiToken, actor.token_id)
        if token is not None:
            label = token.mcp_client_label or token.name
            client_type = token.mcp_client_type or "api_token"
            return (
                f"{label} ({token.id})",
                client_type,
                _brand_for(client_type),
            )
        raise RuntimeError("authenticated PAT row is missing")
    if actor.token_kind == "oauth_jwt" and actor.client_id:
        name = f"OAuth client {actor.client_id}"
        # One row per (OAuth client, user, grant). The stable binding key is
        # authoritative; the suffix also protects the legacy display-name
        # uniqueness constraint from conflating independent grants.
        name = f"{name} (user {actor.user_id}) (grant {actor.token_id})"
        return name, "oauth", None
    raise PermissionError(
        "unsupported_mcp_credential: MCP requires a PAT or OAuth bearer"
    )


def _legacy_oauth_trace_name(actor: ActorContext) -> str | None:
    if actor.token_kind != "oauth_jwt" or actor.token_id is None or not actor.client_id:
        return None
    name = f"OAuth client {actor.client_id[:24]}"
    if actor.user_id is not None:
        name = f"{name} (user {str(actor.user_id)[:8]})"
    return name


def _brand_for(client_type: str | None) -> str | None:
    if client_type is None:
        return None
    app = get_mcp_app_catalog_repository().find_app(client_type)
    if app is None:
        return None
    return app.runtime_brand or app.id
