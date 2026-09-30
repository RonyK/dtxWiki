from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from backend.policy.auth import ActorContext
from backend.services.mcp_chat_service import fetch_conversation_chats

NEW_CONVERSATION_SENTINEL = "new"


@dataclass(frozen=True)
class ConversationResolution:
    token: str
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None


def mint_conversation_token() -> str:
    return f"conv_{uuid4().hex}"


def resolve_conversation_token(
    *,
    session_factory,
    actor: ActorContext,
    requested_token: Any,
    requested_workspace_id: UUID | None = None,
    requested_workspace_authorized: bool,
    require_token: bool = False,
) -> ConversationResolution:
    # C-03: no default — every caller must PROVE the requested workspace was
    # authorized. A permissive default would let a future caller silently skip
    # the cross-workspace token-validation guard (fail-open).
    normalized, normalize_error = _normalize_requested_token(requested_token)
    if normalize_error is not None:
        return ConversationResolution(
            token=mint_conversation_token(),
            error_code=normalize_error,
        )
    if normalized is None:
        # Omitting the token used to mean "start a new conversation", which
        # silently split one client chat into a chat per call: no standard MCP
        # client echoes a dtxWiki-specific envelope field, and the transport
        # session id is not a substitute (it changes on every ephemeral exec,
        # reconnect and re-registration). Content calls must now say which
        # conversation they belong to; "new" starts one explicitly.
        if require_token and not _is_new_sentinel(requested_token):
            return ConversationResolution(
                token=mint_conversation_token(),
                error_code="conversation_token_required",
            )
        return ConversationResolution(token=mint_conversation_token())
    if not requested_workspace_authorized:
        # Do not query an unauthorized shard to validate token ownership. Keep
        # cross-workspace, unknown-token, and wrong-owner cases indistinguishable.
        return ConversationResolution(
            token=mint_conversation_token(),
            error_code="conversation_token_unknown",
        )
    if session_factory is None:
        return ConversationResolution(token=normalized)

    # Workspace-scope the lookup, then collapse every non-owned case
    # (token unknown anywhere, owned by another actor, or outside the
    # resolved workspace) to a single error code so the resolution never
    # acts as an existence oracle for tokens in other workspaces.
    workspace_id = requested_workspace_id or actor.workspace_id
    chats = [
        chat
        for chat in fetch_conversation_chats(session_factory, normalized)
        if workspace_id is None or chat.workspace_id == workspace_id
    ]

    if actor.user_id is not None and any(
        chat.owner_user_id == actor.user_id for chat in chats
    ):
        return ConversationResolution(token=normalized)
    return ConversationResolution(
        token=mint_conversation_token(),
        error_code="conversation_token_unknown",
    )


def _is_new_sentinel(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.strip().lower() == NEW_CONVERSATION_SENTINEL
    )


def _normalize_requested_token(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "conversation_token_malformed"
    normalized = value.strip()
    if not normalized or normalized.lower() == NEW_CONVERSATION_SENTINEL:
        return None, None
    if len(normalized) > 160:
        return None, "conversation_token_malformed"
    return normalized, None
