from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage
from backend.models.context import ContextPage
from backend.models.favorite import UserFavorite
from backend.models.library import LibraryItem
from backend.policy.auth import ActorContext
from backend.schemas.tree import FavoritePayload, FavoriteTargetKind
from backend.services.actor_visible_read import (
    agent_page_visible,
    context_page_visible,
    library_item_visible,
)
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.read_errors import ReadNotFoundError, ScopeDeniedError


def set_favorite(
    session: Session,
    *,
    workspace_id: UUID,
    actor: ActorContext,
    target_kind: FavoriteTargetKind,
    target_id: UUID,
    is_favorite: bool,
) -> FavoritePayload:
    if actor.user_id is None:
        raise ScopeDeniedError("personal favorites require an authenticated user")
    targets = {
        "context_page": (ContextPage, context_page_visible),
        "library_item": (LibraryItem, library_item_visible),
        "agent_page": (AgentPage, agent_page_visible),
    }
    model, visible = targets[target_kind]
    acquire_workspace_write_intent(session)
    # Lock the target before preference creation to serialize concurrent PUTs
    # for this principal/target on PostgreSQL as well as SQLite.
    target = session.scalar(
        select(model)
        .where(model.workspace_id == workspace_id, model.id == target_id)
        .with_for_update()
    )
    if not visible(target, actor, workspace_id=workspace_id):
        raise ReadNotFoundError("favorite target not found")
    favorite = session.get(
        UserFavorite, (actor.user_id, workspace_id, target_kind, target_id)
    )
    if is_favorite and favorite is None:
        session.add(
            UserFavorite(
                user_id=actor.user_id,
                workspace_id=workspace_id,
                target_kind=target_kind,
                target_id=target_id,
            )
        )
    elif not is_favorite and favorite is not None:
        session.delete(favorite)
    session.flush()
    return FavoritePayload(
        target_kind=target_kind, target_id=target_id, is_favorite=is_favorite
    )
